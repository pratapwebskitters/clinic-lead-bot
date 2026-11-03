from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import db, pipeline
from app.main import app
from app.services import followup, notify, recovery, router, workers
from tests.conftest import HEADERS, lead_payload


def save(status="received", age_minutes=0):
    lead_id = db.save_lead(**lead_payload(service=None), status=status)
    timestamp = db.iso(db.utcnow() - timedelta(minutes=age_minutes))
    with db.connect() as conn:
        conn.execute("UPDATE leads SET created_at=?, status_updated_at=? WHERE id=?",
                     (timestamp, timestamp, lead_id))
    return lead_id


def test_old_received_lead_is_reprocessed_once(client, sent, fake_llm, auto_mode):
    old = save(age_minutes=3)
    fresh = save(age_minutes=1)
    calls = fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    recovery.run_recovery()
    recovery.run_recovery()
    workers.wait_for_idle()
    assert db.get_lead(old)["status"] == "replied"
    assert db.get_lead(fresh)["status"] == "received"
    assert calls["n"] == 1
    assert len(sent["email"]) == 1


@pytest.mark.parametrize("status", ["processing", "sending"])
def test_stale_claims_escalate_without_resending(client, sent, fake_llm, status):
    old = save(status, 11)
    fresh = save(status, 9)
    calls = fake_llm({"score": "cold", "reply": "Should not send."})
    recovery.run_recovery()
    recovery.run_recovery()
    lead = db.get_lead(old)
    assert lead["status"] == ("delivery_unknown" if status == "sending" else "needs_human")
    assert lead["needs_human"] == 1
    assert lead["owner_notified"] == 1 and lead["notify_attempts"] == 1
    assert db.get_lead(fresh)["status"] == status
    assert calls["n"] == 0 and sent["email"] == []
    assert len(sent["telegram"]) == 1
    assert sent["telegram"][0][0] == "admin-chat"


def test_recovery_uses_claim_age_not_creation_age(client, sent):
    lead_id = save(age_minutes=60)
    assert db.claim_status(lead_id, "received", "processing")
    recovery.run_recovery()
    assert db.get_lead(lead_id)["status"] == "processing"
    assert sent["telegram"] == []


def test_recovery_runs_at_startup_without_scheduler(sent, fake_llm, auto_mode, monkeypatch):
    monkeypatch.setenv("ENABLE_RECOVERY", "1")
    db.init_db()
    lead_id = save(age_minutes=3)
    fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    tick_done = Event()
    original = recovery.run_recovery

    def tick():
        original()
        tick_done.set()

    monkeypatch.setattr(recovery, "run_recovery", tick)
    with TestClient(app):
        assert tick_done.wait(timeout=5)
        workers.wait_for_idle()
        assert db.get_lead(lead_id)["status"] == "replied"
    assert len(sent["email"]) == 1


def test_recovery_is_registered_with_scheduler(monkeypatch):
    scheduler = MagicMock()
    monkeypatch.setenv("ENABLE_SCHEDULER", "1")
    monkeypatch.setenv("ENABLE_RECOVERY", "1")
    monkeypatch.setenv("FOLLOWUP_INTERVAL_MINUTES", "7")
    monkeypatch.setattr("apscheduler.schedulers.background.BackgroundScheduler",
                        lambda: scheduler)
    tick_done = Event()
    worker = MagicMock(side_effect=lambda: tick_done.set())
    monkeypatch.setattr(recovery, "run_recovery", worker)
    with TestClient(app):
        assert tick_done.wait(timeout=5)
        worker.assert_called_once_with()
    scheduler.add_job.assert_called_once_with(followup.run_followups, "interval", minutes=7)
    scheduler.start.assert_called_once()
    scheduler.shutdown.assert_called_once_with(wait=False)


def test_failed_shadow_alert_is_persisted_and_retried(client, sent, fake_llm, monkeypatch):
    fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    original_send = notify.send_telegram
    monkeypatch.setattr(notify, "send_telegram", lambda *args: False)
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    lead = db.get_lead(lead_id)
    assert lead["status"] == "pending_approval"
    assert lead["owner_notified"] == 0 and lead["notify_attempts"] == 1
    monkeypatch.setattr(notify, "send_telegram", original_send)
    recovery.run_recovery()
    recovery.run_recovery()
    lead = db.get_lead(lead_id)
    assert lead["status"] == "pending_approval"
    assert lead["owner_notified"] == 1 and lead["notify_attempts"] == 2
    assert len(sent["telegram"]) == 1
    assert "SHADOW MODE" in sent["telegram"][0][1]
    assert sent["email"] == []


def test_alert_attempts_are_bounded(client, sent, fake_llm, monkeypatch, caplog):
    fake_llm({"score": "hot", "reply": "Thanks for contacting us."})
    sender = MagicMock(return_value=False)
    monkeypatch.setattr(notify, "send_telegram", sender)
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    for _ in range(8):
        recovery.run_recovery()
    lead = db.get_lead(lead_id)
    assert sender.call_count == 5
    assert lead["notify_attempts"] == 5 and lead["owner_notified"] == 0
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert "retries exhausted" in caplog.text


def test_pipeline_failure_alert_is_retried_to_admin(client, sent, monkeypatch):
    lead_id = save()
    original_send = notify.send_telegram
    monkeypatch.setattr(notify, "send_telegram", lambda *args: False)
    monkeypatch.setattr(pipeline, "_process", MagicMock(side_effect=RuntimeError("failure")))
    pipeline.process_lead(lead_id)
    assert db.get_lead(lead_id)["owner_notified"] == 0
    monkeypatch.setattr(notify, "send_telegram", original_send)
    recovery.run_recovery()
    lead = db.get_lead(lead_id)
    assert lead["owner_notified"] == 1 and lead["notify_attempts"] == 2
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert sent["telegram"][0][0] == "admin-chat"


def test_llm_failure_in_shadow_mode_requires_human(client, sent, fake_llm):
    fake_llm(RuntimeError("LLM unavailable"))
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    lead = db.get_lead(lead_id)
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert sent["email"] == [] and sent["telegram"]


@pytest.mark.parametrize("mode", ["auto", "shadow"])
def test_urgent_spam_is_escalated_with_safe_reply(client, sent, fake_llm, auto_mode, mode):
    auto_mode["mode"] = mode
    fake_llm({"score": "spam", "urgent": True, "reply": "MODEL REPLY MUST NOT BE SENT"})
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    lead = db.get_lead(lead_id)
    assert lead["score"] == "hot" and lead["status"] != "spam"
    assert lead["urgent"] == 1 and lead["needs_human"] == 1
    assert "MODEL REPLY" not in lead["reply"]
    assert sent["telegram"] and "URGENT" in sent["telegram"][0][1]
    if mode == "auto":
        assert len(sent["email"]) == 1
        assert sent["email"][0]["body"] == lead["reply"]
    else:
        assert lead["status"] == "pending_approval"
        assert sent["email"] == []


def test_needs_human_status_always_sets_flag(client):
    lead_id = save()
    db.update_lead(lead_id, status="needs_human", needs_human=0)
    assert db.get_lead(lead_id)["needs_human"] == 1
    other = save()
    assert db.claim_status(other, "received", "needs_human")
    assert db.get_lead(other)["needs_human"] == 1


def test_crash_during_last_alert_attempt_escalates_without_retry(client, sent, caplog):
    lead_id = save("pending_approval")
    db.queue_alert(lead_id, "admin-chat", "Draft")
    with db.connect() as conn:
        conn.execute("UPDATE leads SET notify_attempts=5, notify_claimed_at=? WHERE id=?",
                     (db.iso(db.utcnow() - timedelta(minutes=11)), lead_id))
    recovery.run_recovery()
    lead = db.get_lead(lead_id)
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert lead["notify_attempts"] == 5 and lead["owner_notified"] == 0
    assert sent["telegram"] == []
    assert "retries exhausted" in caplog.text


def test_successful_cold_reply_does_not_generate_recovery_alert(client, sent, fake_llm, auto_mode):
    fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    recovery.run_recovery()
    assert db.get_lead(lead_id)["notify_attempts"] == 0
    assert sent["telegram"] == []


def test_alert_claim_prevents_concurrent_retry(client, sent, monkeypatch):
    lead_id = save("pending_approval")
    db.queue_alert(lead_id, "owner-chat", "Draft")
    original_send = notify.send_telegram
    started, release = Event(), Event()

    def blocked_send(chat_id, text):
        started.set()
        assert release.wait(timeout=10)
        return original_send(chat_id, text)

    monkeypatch.setattr(notify, "send_telegram", blocked_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(router.send_pending_alert, lead_id)
        try:
            assert started.wait(timeout=10)
            recovery.run_recovery()
        finally:
            release.set()
        future.result(timeout=10)
    assert sent["telegram"] == [("owner-chat", "Draft")]
    assert db.get_lead(lead_id)["notify_attempts"] == 1
    assert db.get_lead(lead_id)["owner_notified"] == 1
