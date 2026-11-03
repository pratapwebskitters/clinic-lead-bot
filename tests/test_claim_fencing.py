from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest

from app import db, pipeline
from app.models import QualificationResult
from app.services import followup, llm, mailer, notify, recovery, router
from tests.conftest import lead_payload


def save(status="received"):
    return db.save_lead(**lead_payload(service=None), status=status)


@pytest.mark.parametrize("mode", ["auto", "shadow"])
def test_qualification_resuming_after_recovery_cannot_send(client, sent, auto_mode, monkeypatch, caplog, mode):
    caplog.set_level("INFO")
    auto_mode["mode"] = mode
    lead_id = save()
    entered, release = Event(), Event()

    def qualify(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return QualificationResult(score="hot", reply="Our team can help you book.")

    monkeypatch.setattr(llm, "qualify", qualify)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pipeline.process_lead, lead_id)
        try:
            assert entered.wait(timeout=5)
            token = db.get_lead(lead_id)["claim_token"]
            assert str(UUID(token)) == token
            recovery.run_recovery(now=db.utcnow() + timedelta(minutes=11))
            escalated = db.get_lead(lead_id)
            assert escalated["status"] == "needs_human" and escalated["claim_token"] is None
        finally:
            release.set()
        future.result(timeout=5)
    lead = db.get_lead(lead_id)
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert lead["reply"] is None and lead["score"] is None
    assert lead["reason"] == escalated["reason"]
    assert lead["sent_at"] is None and lead["replied_at"] is None
    assert sent["email"] == []
    assert len(sent["telegram"]) == 1 and "SHADOW MODE" not in sent["telegram"][0][1]
    assert "lost claim" in caplog.text


def test_replaced_token_fences_preparation_and_begin_send(client):
    lead_id = save()
    old = db.claim_processing(lead_id)
    assert old and db.transition(lead_id, "processing", "needs_human", {})
    assert db.get_lead(lead_id)["claim_token"] is None
    assert db.transition(lead_id, "needs_human", "received", {})
    current = db.claim_processing(lead_id)
    assert current and current != old
    assert not db.update_claimed(lead_id, old, {"reason": "stale worker wrote this"})
    assert not db.transition(lead_id, "processing", "spam", {}, claim_token=old)
    assert not db.begin_send(lead_id, old)
    assert db.update_claimed(lead_id, current, {"score": "warm"})
    assert db.get_lead(lead_id)["reason"] is None
    assert db.begin_send(lead_id, current)
    assert not db.begin_send(lead_id, current)
    assert db.get_lead(lead_id)["status"] == "sending"


@pytest.mark.parametrize("path", ["auto", "approval", "followup"])
def test_smtp_success_after_escalation_preserves_evidence_and_alerts(client, sent, fake_llm, auto_mode,
                                                                  monkeypatch, path):
    entered, release = Event(), Event()
    lead_id = save("pending_approval" if path == "approval" else "replied" if path == "followup" else "received")
    db.update_lead(lead_id, score="hot", reply="Our team can help you book.")
    if path == "approval":
        token = db.claim_processing(lead_id, "pending_approval")
        lead = db.get_lead(lead_id)
        assert lead["claim_token"] == token
        run = lambda: router.approve_lead(lead)
    elif path == "followup":
        db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
        run = followup.run_followups
    else:
        fake_llm({"score": "hot", "reply": "Our team can help you book."})
        run = lambda: pipeline.process_lead(lead_id)
    original_send = mailer.send_email

    def smtp(**kwargs):
        lead = db.get_lead(lead_id)
        assert lead["status"] == "sending" and lead["claim_token"]
        entered.set()
        assert release.wait(timeout=5)
        return original_send(**kwargs)

    monkeypatch.setattr(mailer, "send_email", smtp)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run)
        try:
            assert entered.wait(timeout=5)
            recovery.run_recovery(now=db.utcnow() + timedelta(minutes=11))
            lead = db.get_lead(lead_id)
            assert lead["status"] == "delivery_unknown" and lead["claim_token"] is None
            assert lead["sent_at"] is None
        finally:
            release.set()
        future.result(timeout=5)
    lead = db.get_lead(lead_id)
    assert lead["status"] == "delivery_unknown" and lead["needs_human"] == 1
    assert lead["claim_token"] is None
    assert lead["sent_at"] and lead["replied_at"] == lead["sent_at"]
    assert len(sent["email"]) == 1
    assert len(sent["telegram"]) == 2
    assert f"An automatic reply WAS sent to priya@example.com at {lead['sent_at']}" in lead["notify_text"]
    assert "while this lead was escalated, check before contacting them." in lead["notify_text"]
    if path == "followup":
        assert lead["followup_status"] == "sent" and lead["followup_sent_at"] == lead["sent_at"]
        assert followup.run_followups() == 0
    recovery.run_recovery()
    assert len(sent["email"]) == 1


def test_normal_reply_is_claimed_before_smtp_and_records_both_timestamps(client, sent, fake_llm,
                                                                      auto_mode, monkeypatch):
    lead_id = save()
    fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    original_send = mailer.send_email

    def smtp(**kwargs):
        lead = db.get_lead(lead_id)
        assert lead["status"] == "sending" and lead["claim_token"]
        return original_send(**kwargs)

    monkeypatch.setattr(mailer, "send_email", smtp)
    pipeline.process_lead(lead_id)
    lead = db.get_lead(lead_id)
    assert lead["status"] == "replied"
    assert lead["sent_at"] and lead["replied_at"] == lead["sent_at"]
    assert lead["claim_token"] is None
    assert len(sent["email"]) == 1


def test_shadow_draft_takes_send_claim_before_queueing(client, sent, fake_llm, monkeypatch):
    lead_id = save()
    fake_llm({"score": "hot", "reply": "Thanks for contacting us."})
    original_begin, original_telegram = db.begin_send, notify.send_telegram
    began = []

    def begin(number, token):
        result = original_begin(number, token)
        began.append((number, token, result))
        return result

    def telegram(chat_id, text, deadline=None):
        lead = db.get_lead(lead_id)
        assert began == [(lead_id, lead["claim_token"], True)]
        assert lead["status"] == "pending_approval"
        assert lead["notify_claim_token"] == lead["claim_token"]
        return original_telegram(chat_id, text, deadline=deadline)

    monkeypatch.setattr(db, "begin_send", begin)
    monkeypatch.setattr(notify, "send_telegram", telegram)
    pipeline.process_lead(lead_id)
    assert len(sent["telegram"]) == 1 and sent["email"] == []


def test_shadow_draft_cannot_send_after_approval_replaces_claim(client, sent, fake_llm, monkeypatch):
    lead_id = save()
    fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    queued, release = Event(), Event()
    original_pending = router.send_pending_alert

    def pending(number, deadline=None):
        queued.set()
        assert release.wait(timeout=5)
        original_pending(number, deadline=deadline)

    monkeypatch.setattr(router, "send_pending_alert", pending)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(pipeline.process_lead, lead_id)
        try:
            assert queued.wait(timeout=5)
            old = db.get_lead(lead_id)["claim_token"]
            current = db.claim_processing(lead_id, "pending_approval")
            assert current and current != old
        finally:
            release.set()
        future.result(timeout=5)
    assert sent["telegram"] == [] and sent["email"] == []
    assert db.get_lead(lead_id)["claim_token"] == current
    assert db.get_lead(lead_id)["status"] == "processing"


def test_followup_lost_claim_before_smtp_does_not_send(client, sent, monkeypatch, caplog):
    caplog.set_level("INFO")
    lead_id = save("replied")
    db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    entered, release = Event(), Event()
    original_text = followup.followup_text

    def text(lead, clinic):
        entered.set()
        assert release.wait(timeout=5)
        return original_text(lead, clinic)

    monkeypatch.setattr(followup, "followup_text", text)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(followup.run_followups)
        try:
            assert entered.wait(timeout=5)
            assert db.get_lead(lead_id)["status"] == "processing"
            recovery.run_recovery(now=db.utcnow() + timedelta(minutes=11))
        finally:
            release.set()
        assert future.result(timeout=5) == 0
    lead = db.get_lead(lead_id)
    assert lead["status"] == "delivery_unknown" and lead["claim_token"] is None
    assert lead["needs_human"] == 1
    assert lead["followup_sent"] == 1 and lead["followup_status"] == "uncertain"
    assert sent["email"] == [] and "lost claim" in caplog.text


def test_old_watchdog_cannot_revoke_a_replacement_claim(client):
    lead_id = save()
    old = db.claim_processing(lead_id)
    assert db.transition(lead_id, "processing", "needs_human", {})
    assert db.transition(lead_id, "needs_human", "received", {})
    current = db.claim_processing(lead_id)
    assert not db.mark_worker_abandoned(lead_id, old, "Stale watchdog")
    lead = db.get_lead(lead_id)
    assert lead["claim_token"] == current and lead["worker_abandoned"] == 0
    assert lead["status"] == "processing"


def test_late_sent_evidence_and_alert_commit_together(client, monkeypatch):
    lead_id = save()
    token = db.claim_processing(lead_id)
    assert db.begin_send(lead_id, token)
    recovery.run_recovery(now=db.utcnow() + timedelta(minutes=11))
    original = db._queue_alert

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash before commit")

    timestamp = db.iso(db.utcnow())
    with monkeypatch.context() as patch:
        patch.setattr(db, "_queue_alert", crash)
        with pytest.raises(RuntimeError, match="crash before commit"):
            db.record_send(lead_id, token, "sent", timestamp, {}, "owner", None, "WAS sent")
    assert db.get_lead(lead_id)["sent_at"] is None
    status, queued = db.record_send(lead_id, token, "sent", timestamp, {}, "owner", None, "WAS sent")
    assert queued and status == "delivery_unknown"
    assert db.get_lead(lead_id)["sent_at"] == timestamp
    assert db.get_lead(lead_id)["notify_text"] == "WAS sent"
