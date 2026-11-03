"""Exhaust every boundary of each workflow using process-termination faults.

BaseException deliberately bypasses application error handlers, just as a killed
process would. SQLite and captured remote deliveries survive each simulated restart;
worker memory does not. All external services are supplied by the shared fixtures.
"""
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from app import clinics, db, pipeline
from app.services import followup, mailer, recovery, router, workers
from tests.conftest import lead_payload


class ProcessCrash(BaseException):
    pass


SCENARIOS = ("hot_auto", "shadow", "urgent", "llm_failure", "spam",
             "warm_followup", "approval", "uncertain_send")


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_crash_boundary(scenario, tmp_path, monkeypatch, sent, fake_llm):
    clinic = dict(clinics.load_clinic("demo_clinic"),
                  mode="shadow" if scenario in {"shadow", "approval"} else "auto")
    monkeypatch.setattr(clinics, "load_clinic", lambda _: clinic)
    # Deterministic dispatch executes the same pipeline, with no surviving executor,
    # monitor, queued Future or watchdog state to rescue a crashed process.
    monkeypatch.setattr(workers, "submit_lead", lambda lead_id, **_: pipeline.process_lead(lead_id))
    start = datetime(2030, 1, 1, tzinfo=timezone.utc)
    clock = [start]
    monkeypatch.setattr(db, "utcnow", lambda: clock[0])
    captured_email = mailer.send_email

    if scenario == "uncertain_send":
        def uncertain(**kwargs):
            captured_email(**kwargs)  # SMTP may have accepted the message.
            return mailer.DeliveryResult.UNCERTAIN
        monkeypatch.setattr(mailer, "send_email", uncertain)

    def setup(run):
        monkeypatch.setattr(db, "fault_hook", lambda _: None)
        workers.stop()
        sent["email"].clear()
        sent["telegram"].clear()
        clock[0] = start
        monkeypatch.setenv("DB_PATH", str(tmp_path / f"{scenario}-{run}.db"))
        db.init_db()
        output = {"score": "warm" if scenario == "warm_followup" else
                  "spam" if scenario == "spam" else "hot",
                  "urgent": scenario == "urgent", "reply": "Our team can help you book."}
        fake_llm(RuntimeError("provider unavailable") if scenario == "llm_failure" else output)
        # The invariant begins once the webhook has durably accepted the lead.
        lead_id = db.save_lead(**lead_payload(service=None))
        if scenario == "approval":
            pipeline.process_lead(lead_id)
            assert db.get_lead(lead_id)["status"] == "pending_approval"
        return lead_id

    def workflow(lead_id):
        if scenario == "approval":
            token = db.claim_processing(lead_id, "pending_approval")
            lead = db.get_lead(lead_id)
            lead["claim_token"] = token
            router.approve_lead(lead)
        else:
            pipeline.process_lead(lead_id)
        if scenario == "warm_followup":
            clock[0] += timedelta(days=2)
            followup.run_followups()

    def restart_and_check(lead_id):
        monkeypatch.setattr(db, "fault_hook", lambda _: None)
        workers.stop()
        assert workers.stats()["active"] == 0 and workers.stats()["queued"] == 0
        db.init_db()  # Reopen/migrate persistent state as a new process would.
        for _ in range(7):
            clock[0] += timedelta(days=2)
            recovery.run_recovery()
            followup.run_followups()
        lead = db.get_lead(lead_id)
        assert lead is not None
        assert lead["status"] in {"replied", "spam", "needs_human", "delivery_unknown", "pending_approval"}
        assert lead["followup_status"] != "claimed"
        if lead["needs_human"] or lead["status"] in {"pending_approval", "delivery_unknown"}:
            assert lead["notify_generation"] >= 1 and lead["notify_text"]
            assert lead["owner_notified"] or lead["notify_attempts"] > 0
        counts = Counter((email["to"], "followup" if email["subject"].startswith("Following up:")
                          else "reply") for email in sent["email"])
        assert all(count <= 1 for count in counts.values()), counts
        assert len(sent["telegram"]) <= 3, sent["telegram"]
        assert lead["notify_generation"] <= 3 and lead["notify_attempts"] <= 5
        stable = (lead["notify_generation"], len(sent["email"]), len(sent["telegram"]))
        for _ in range(3):
            clock[0] += timedelta(days=2)
            recovery.run_recovery()
            followup.run_followups()
        assert stable == (db.get_lead(lead_id)["notify_generation"],
                          len(sent["email"]), len(sent["telegram"]))

    # Discover N from the complete, successful path instead of a magic upper bound.
    lead_id = setup("baseline")
    boundaries = []
    monkeypatch.setattr(db, "fault_hook", boundaries.append)
    workflow(lead_id)
    restart_and_check(lead_id)
    expected_emails = 0 if scenario in {"spam", "shadow"} else 2 if scenario == "warm_followup" else 1
    assert len(sent["email"]) == expected_emails
    if scenario == "llm_failure":
        assert db.get_lead(lead_id)["status"] == "needs_human"
    if scenario == "uncertain_send":
        assert db.get_lead(lead_id)["status"] == "delivery_unknown"
    assert "before_send:llm" in boundaries or scenario == "approval"
    if expected_emails:
        assert {"before_send:email", "after_send:email"} <= set(boundaries)
    assert {"before_commit", "after_commit"} <= set(boundaries)
    if scenario != "spam":
        assert "before_send:telegram" in boundaries or scenario == "warm_followup"
    failures = []
    for index, boundary in enumerate(boundaries, 1):
        lead_id = setup(index)
        calls = []

        def crash(name):
            calls.append(name)
            if len(calls) == index:
                raise ProcessCrash(name)

        monkeypatch.setattr(db, "fault_hook", crash)
        try:
            with pytest.raises(ProcessCrash):
                workflow(lead_id)
            assert calls[-1] == boundary
            restart_and_check(lead_id)
        except Exception as exc:
            failures.append(f"hook {index}/{len(boundaries)} ({boundary}): {exc!r}")
        finally:
            monkeypatch.setattr(db, "fault_hook", lambda _: None)
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("boundary,persisted", [("before_commit", False), ("after_commit", True)])
def test_save_commit_boundary_survives_process_crash(tmp_path, monkeypatch, boundary, persisted):
    db.init_db()

    def crash(name):
        if name == boundary:
            raise ProcessCrash(name)

    with monkeypatch.context() as patch:
        patch.setattr(db, "fault_hook", crash)
        with pytest.raises(ProcessCrash):
            db.save_lead(**lead_payload(service=None))
    with db.connect() as conn:
        rows = conn.execute("SELECT * FROM leads").fetchall()
    assert bool(rows) is persisted
    if persisted:
        assert rows[0]["status"] == "received"
