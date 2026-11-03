import json
from datetime import timedelta
from queue import Queue
from threading import Event, Lock
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from app import clinics, db, pipeline
from app.config import get_settings
from app.main import app
from app.services import followup, llm, mailer, recovery, router, safety, workers
from tests.conftest import HEADERS, lead_payload


def save(status="received", age=0):
    lead_id = db.save_lead(**lead_payload(service=None), status="received" if status == "processing" else status)
    if status == "processing":
        assert db.claim_processing(lead_id)
    age_lead(lead_id, age)
    return lead_id


def age_lead(lead_id, minutes):
    timestamp = db.iso(db.utcnow() - timedelta(minutes=minutes))
    with db.connect() as conn:
        conn.execute("UPDATE leads SET created_at=?, status_updated_at=? WHERE id=?",
                     (timestamp, timestamp, lead_id))


def test_recovery_later_tick_without_followup_scheduler(sent, fake_llm, auto_mode, monkeypatch):
    db.init_db()
    received, processing = save(age=1), save("processing", age=9)
    calls = fake_llm({"score": "cold", "reply": "Our team will contact you."})
    monkeypatch.setenv("ENABLE_RECOVERY", "1")
    monkeypatch.setenv("ENABLE_SCHEDULER", "0")
    worker = recovery.RecoveryWorker()
    tick_finished, advance = Queue(), Event()
    intervals = []

    def wait(seconds):
        intervals.append(seconds)
        tick_finished.put(True)
        assert advance.wait(timeout=5)
        advance.clear()
        return worker.stopping.is_set()

    monkeypatch.setattr(worker.stopping, "wait", wait)
    monkeypatch.setattr(recovery, "RecoveryWorker", lambda: worker)
    try:
        with TestClient(app):
            try:
                assert tick_finished.get(timeout=5)
                assert db.get_lead(received)["status"] == "received"
                assert db.get_lead(processing)["status"] == "processing"
                assert calls["n"] == 0
                age_lead(received, 3)
                age_lead(processing, 11)
                advance.set()
                assert tick_finished.get(timeout=5)
                workers.wait_for_idle()
                assert db.get_lead(received)["status"] == "replied"
                assert db.get_lead(processing)["status"] == "needs_human"
                assert db.get_lead(processing)["needs_human"] == 1
                assert len(sent["email"]) == 1 and sent["telegram"]
                assert calls["n"] == 1 and len(intervals) == 2
                assert all(0 < interval <= 60 for interval in intervals)
            finally:
                worker.stopping.set()
                advance.set()
    finally:
        worker.stopping.set()
        advance.set()


def test_startup_is_ready_while_recovery_is_blocked(monkeypatch):
    monkeypatch.setenv("ENABLE_RECOVERY", "1")
    started, release, ended = Event(), Event(), Event()

    def slow_recovery():
        started.set()
        release.wait(timeout=5)
        ended.set()

    monkeypatch.setattr(recovery, "run_recovery", slow_recovery)
    with TestClient(app) as client:
        try:
            assert started.wait(timeout=5)
            assert not ended.is_set()
            assert client.get("/health").status_code == 200
        finally:
            release.set()


def test_worker_pool_is_bounded_and_coalesces_pending_jobs(monkeypatch, sent):
    db.init_db()
    started, release, lock = Event(), Event(), Lock()
    counts = {"active": 0, "peak": 0, "finished": 0}

    def process(_):
        with lock:
            counts["active"] += 1
            counts["peak"] = max(counts["peak"], counts["active"])
            if counts["active"] == 2:
                started.set()
        assert release.wait(timeout=5)
        with lock:
            counts["active"] -= 1
            counts["finished"] += 1

    monkeypatch.setattr(pipeline, "process_lead", process)
    workers.start(2)
    try:
        futures = [workers.submit_lead(index) for index in range(4)]
        assert started.wait(timeout=5)
        assert all(future is not None for future in futures)
        assert workers.submit_lead(0) is futures[0]
        assert workers.submit_lead(5) is None
        release.set()
        workers.wait_for_idle()
        assert counts == {"active": 0, "peak": 2, "finished": 4}
    finally:
        release.set()
        workers.stop()


def test_recovery_and_worker_defaults(monkeypatch):
    monkeypatch.delenv("ENABLE_RECOVERY", raising=False)
    monkeypatch.delenv("LEAD_WORKERS", raising=False)
    assert get_settings().enable_recovery is True
    assert get_settings().lead_workers == 4


@pytest.mark.parametrize("app_env", ["development", "production"])
@pytest.mark.parametrize("name", ["MyClinic.yaml", "my clinic.yaml"])
def test_startup_rejects_unreachable_clinic_filenames(tmp_path, monkeypatch, caplog, app_env, name):
    data = clinics.load_clinic("demo_clinic")
    (tmp_path / name).write_text(yaml.safe_dump(data), encoding="utf-8")
    monkeypatch.setenv("CLINICS_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", app_env)
    if app_env == "production":
        with pytest.raises(clinics.ClinicConfigError, match="filename stem") as exc:
            with TestClient(app):
                pass
        assert name in str(exc.value)
    else:
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
        assert name in caplog.text and "filename stem" in caplog.text


def test_sliding_dedup_returns_original_root(client):
    payload = lead_payload(service=None)
    original, _ = db.save_lead_dedup(**payload)
    duplicate, root = db.save_lead_dedup(**payload)
    assert root == original
    age_lead(original, 25 * 60)
    age_lead(duplicate, 2 * 60)
    new_id, root = db.save_lead_dedup(**payload)
    assert root == original
    assert db.get_lead(new_id)["is_duplicate"] == 1
    assert db.get_lead(new_id)["duplicate_of"] == original
    assert db.find_duplicate(clinic_id=payload["clinic_id"], phone=payload["phone"],
                             email=payload["email"])["id"] == original


def test_sliding_dedup_resolves_legacy_duplicate_without_root(client):
    original = save(age=25 * 60)
    duplicate = db.save_lead(**lead_payload(service=None), status="duplicate", is_duplicate=True)
    age_lead(duplicate, 2 * 60)
    _, root = db.save_lead_dedup(**lead_payload(service=None))
    assert root == original


def test_repeat_question_is_alerted_once_without_llm_or_reply(client, sent, fake_llm):
    calls = fake_llm({"score": "cold", "reply": "Our team will contact you."})
    first = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()
    sent["telegram"].clear()
    for _ in range(2):
        response = client.post("/webhook/lead", json=lead_payload(message="Are you open on Sunday?"),
                               headers=HEADERS).json()
        assert response["status"] == "duplicate" and response["duplicate_of"] == first["lead_id"]
    assert len(sent["telegram"]) == 1
    assert "Repeat enquiry from Priya with a new message: Are you open on Sunday?" in sent["telegram"][0][1]
    assert "Low priority" in sent["telegram"][0][1]
    assert calls["n"] == 1 and sent["email"] == []
    recovery.run_recovery()
    assert len(sent["telegram"]) == 1


def test_repeat_note_survives_crash_before_queueing(client, sent):
    original, _ = db.save_lead_dedup(**lead_payload(service=None))
    repeat, root = db.save_lead_dedup(**lead_payload(service=None, message="A follow-up question"))
    assert root == original and db.get_lead(repeat)["notify_text"] is None
    recovery.run_recovery()
    recovery.run_recovery()
    assert db.get_lead(repeat)["owner_notified"] == 1
    assert len(sent["telegram"]) == 1 and "A follow-up question" in sent["telegram"][0][1]


@pytest.mark.parametrize("first_duration,deadline,expected_timeouts", [
    (6, 130, [30, 24]), (11, 120, [20]),
])
def test_llm_uses_remaining_budget_and_skips_short_retry(monkeypatch, first_duration, deadline, expected_timeouts):
    clinic = clinics.load_clinic("demo_clinic")
    monkeypatch.setenv("LLM_TIMEOUT", "60")
    clock, timeouts = {"now": 100}, []

    def provider(system, user, timeout=None, deadline=None):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            clock["now"] += first_duration
            raise TimeoutError("slow provider")
        return json.dumps({"score": "cold", "reply": "Thanks."})

    with monkeypatch.context() as patch:
        patch.setattr(llm.time, "monotonic", lambda: clock["now"])
        patch.setattr(llm, "call_provider", provider)
        result = llm.qualify({}, clinic, deadline=deadline)
    assert timeouts == expected_timeouts
    assert result.llm_failed is (len(expected_timeouts) == 1)


def test_expired_provider_result_sends_safe_template_and_marks_human(client, sent, auto_mode, monkeypatch):
    lead_id = save()
    clock, timeouts = {"now": 100}, []

    def provider(system, user, timeout=None, deadline=None):
        timeouts.append(timeout)
        clock["now"] += 46
        return json.dumps({"score": "hot", "reply": "MODEL RESULT ARRIVED TOO LATE"})

    with monkeypatch.context() as patch:
        patch.setattr(llm.time, "monotonic", lambda: clock["now"])
        patch.setattr(llm, "call_provider", provider)
        pipeline.process_lead(lead_id)
    lead = db.get_lead(lead_id)
    assert timeouts == [20]
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert lead["reason"] == "Processing deadline exceeded"
    assert len(sent["email"]) == 1 and sent["email"][0]["body"] == safety.safe_reply(auto_mode)
    assert sent["telegram"]


def test_smtp_timeout_uses_remaining_deadline(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    factory = MagicMock()
    with monkeypatch.context() as patch:
        patch.setattr(mailer.time, "monotonic", lambda: 100)
        patch.setattr(mailer.smtplib, "SMTP", factory)
        outcome = mailer.send_email("patient@example.com", "Enquiry", "Thanks.", deadline=107)
    assert outcome is mailer.DeliveryResult.SENT
    assert factory.call_args.kwargs["timeout"] == 7
    assert factory.return_value.sock.settimeout.call_args_list[0].args == (7,)


@pytest.mark.parametrize("outcome,expected_status,attempts", [
    (mailer.DeliveryResult.FAILED, "needs_human", 2),
    (mailer.DeliveryResult.UNCERTAIN, "delivery_unknown", 1),
    (mailer.DeliveryResult.SENT, "needs_human", 1),
])
def test_smtp_deadline_fallback_never_resends_uncertain_delivery(client, sent, auto_mode, monkeypatch,
                                                               outcome, expected_status, attempts):
    lead_id = save("processing")
    db.update_lead(lead_id, score="cold", reply="Original model reply")
    clock, deliveries = {"now": 100}, []

    def send(**kwargs):
        deliveries.append(kwargs)
        clock["now"] += 46
        return outcome if len(deliveries) == 1 else mailer.DeliveryResult.SENT

    with monkeypatch.context() as patch:
        patch.setattr(mailer.time, "monotonic", lambda: clock["now"])
        patch.setattr(mailer, "send_email", send)
        router.route(db.get_lead(lead_id), auto_mode, score="cold", urgent=False,
                     needs_human=False, reply="Original model reply", deadline=145)
    assert len(deliveries) == attempts
    assert deliveries[0]["deadline"] == 145
    assert db.get_lead(lead_id)["status"] == expected_status
    assert db.get_lead(lead_id)["needs_human"] == 1
    if attempts == 2:
        assert deliveries[1]["body"] == safety.safe_reply(auto_mode)
        assert "deadline" not in deliveries[1]


@pytest.mark.parametrize("outcome,status", [
    (mailer.DeliveryResult.SENT, "sent"), (mailer.DeliveryResult.FAILED, "failed"),
    (mailer.DeliveryResult.UNCERTAIN, "uncertain"),
])
def test_followup_outcomes_are_reported_separately(client, sent, monkeypatch, outcome, status):
    lead_id = save("replied")
    db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    monkeypatch.setattr(mailer, "send_email", MagicMock(return_value=outcome))
    assert followup.run_followups() == int(status == "sent")
    assert db.get_lead(lead_id)["followup_status"] == status
    stats = db.weekly_stats()
    for expected in ("sent", "blocked", "failed", "uncertain"):
        assert stats[f"followups_{expected}"] == int(expected == status)


def test_blocked_followup_does_not_count_as_sent(client, sent, auto_mode):
    lead_id = save("replied")
    db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    auto_mode["clinic_name"] = "Painless clinic"
    assert followup.run_followups() == 0
    lead = db.get_lead(lead_id)
    assert lead["followup_sent"] == 1 and lead["followup_status"] == "blocked"
    stats = db.weekly_stats()
    assert stats["followups_sent"] == 0 and stats["followups_blocked"] == 1
    assert sent["email"] == []


def test_weekly_report_prints_each_outcome(client, monkeypatch, capsys):
    from scripts import weekly_report

    for status in ("sent", "blocked", "failed", "uncertain"):
        lead_id = save("replied")
        db.update_lead(lead_id, followup_sent=1, followup_status=status)
    monkeypatch.setattr(weekly_report.sys, "argv", ["weekly_report.py"])
    weekly_report.main()
    output = capsys.readouterr().out
    assert "Follow-ups sent:       1" in output
    assert "Follow-ups blocked:    1" in output
    assert "Follow-ups failed:     1" in output
    assert "Follow-ups uncertain:  1" in output


def test_background_tick_retries_failed_alert_without_followups(client, sent, monkeypatch):
    # Drive the actual loop through two ticks without sleeping a real minute.
    lead_id = save("pending_approval")
    db.queue_alert(lead_id, "owner-chat", "Draft")
    sender = MagicMock(side_effect=[False, True])
    monkeypatch.setattr("app.services.notify.send_telegram", sender)
    worker = recovery.RecoveryWorker()
    attempts = []

    def advance(_):
        attempts.append(db.get_lead(lead_id)["notify_attempts"])
        return len(attempts) == 2

    monkeypatch.setattr(worker.stopping, "wait", advance)
    worker._run()
    assert attempts == [1, 2] and sender.call_count == 2
    assert db.get_lead(lead_id)["owner_notified"] == 1


def test_failed_scheduler_start_cleans_up_worker_pool(monkeypatch):
    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    scheduler = MagicMock()
    scheduler.start.side_effect = RuntimeError("scheduler startup failed")
    monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler", lambda: scheduler)
    with pytest.raises(RuntimeError, match="scheduler startup failed"):
        with TestClient(app):
            pass
    assert workers._executor is None
    scheduler.shutdown.assert_not_called()
