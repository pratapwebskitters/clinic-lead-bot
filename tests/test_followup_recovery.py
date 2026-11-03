from datetime import timedelta

import pytest

from app import db
from app.services import followup, mailer, recovery
from tests.conftest import lead_payload


def due_lead():
    lead_id = db.save_lead(**lead_payload(service=None), status="replied")
    db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    return lead_id


def test_claim_records_durable_marker(client):
    lead_id = due_lead()
    assert db.claim_followup(lead_id)
    lead = db.get_lead(lead_id)
    assert lead["followup_sent"] == 1
    assert lead["followup_status"] == "claimed" and lead["followup_claimed_at"]


def test_uncertain_finish_crash_rolls_back_and_recovery_alerts_once(client, sent, monkeypatch):
    lead_id = due_lead()
    calls = []

    def smtp(**kwargs):
        calls.append(kwargs)
        return mailer.DeliveryResult.UNCERTAIN

    original = db._queue_alert

    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("crash inside finish_followup")

    monkeypatch.setattr(mailer, "send_email", smtp)
    with monkeypatch.context() as patch:
        patch.setattr(db, "_queue_alert", crash)
        assert followup.run_followups() == 0
    lead = db.get_lead(lead_id)
    assert lead["followup_status"] == "claimed" and lead["status"] == "sending"
    assert lead["notify_text"] is None and lead["notify_generation"] == 0
    later = db.utcnow() + timedelta(minutes=11)
    recovery.run_recovery(later)
    recovery.run_recovery(later)
    lead = db.get_lead(lead_id)
    assert lead["followup_status"] == "uncertain" and lead["status"] == "delivery_unknown"
    assert lead["needs_human"] == 1 and lead["claim_token"] is None
    assert lead["notify_generation"] == 1 and len(sent["telegram"]) == 1
    assert lead["followup_sent_at"] is None and len(calls) == 1


@pytest.mark.parametrize("broken", ["claimed", "uncertain_wrong_status", "uncertain_missing_alert"])
def test_followup_recovery_repairs_once(client, sent, broken):
    lead_id = due_lead()
    db.claim_followup(lead_id)
    if broken != "claimed":
        db.update_lead(lead_id, followup_status="uncertain",
                       status="delivery_unknown" if broken.endswith("missing_alert") else "replied")
    later = db.utcnow() + timedelta(minutes=11)
    for _ in range(3):
        recovery.run_recovery(later)
    lead = db.get_lead(lead_id)
    assert lead["status"] == "delivery_unknown" and lead["followup_status"] == "uncertain"
    assert lead["owner_notified"] == 1 and lead["notify_generation"] == 1
    assert len(sent["telegram"]) == 1 and sent["email"] == []


def test_blocked_followup_not_reported_sent(client, sent, monkeypatch):
    lead_id = due_lead()
    monkeypatch.setattr(followup, "followup_text", lambda *_: "Guaranteed painless cure")
    assert followup.run_followups() == 0
    lead = db.get_lead(lead_id)
    assert lead["status"] == "replied" and lead["followup_status"] == "blocked"
    assert lead["followup_sent_at"] is None
    stats = db.weekly_stats()
    assert stats["followups_sent"] == 0 and stats["followups_blocked"] == 1
