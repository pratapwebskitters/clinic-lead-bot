import smtplib
from datetime import timedelta
from unittest.mock import MagicMock

import pytest
import yaml

from app import clinics, db
from app.services import followup, mailer, recovery, router
from tests.conftest import HEADERS, lead_payload


def save(status="processing"):
    lead_id = db.save_lead(**lead_payload(service=None), status="received" if status == "processing" else status)
    if status == "processing":
        assert db.claim_processing(lead_id)
    return lead_id


@pytest.mark.parametrize("after_queue", [False, True])
def test_status_and_alert_roll_back_together(client, monkeypatch, after_queue):
    lead_id = save()
    original_queue = db._queue_alert

    def crash(conn, *args):
        assert conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()[0] == "pending_approval"
        if after_queue:
            original_queue(conn, *args)
        raise RuntimeError("simulated crash within transaction")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_queue_alert", crash)
        with pytest.raises(RuntimeError, match="simulated crash"):
            db.transition(lead_id, "processing", "pending_approval", {}, "Draft alert")
    lead = db.get_lead(lead_id)
    assert lead["status"] == "processing"
    assert lead["notify_text"] is None
    assert lead["notify_generation"] == 0
    assert db.transition(lead_id, "processing", "pending_approval", {}, "Draft alert")
    lead = db.get_lead(lead_id)
    assert lead["status"] == "pending_approval"
    assert lead["notify_text"] == "Draft alert"
    assert lead["notify_generation"] == 1 and lead["owner_notified"] == 0


def test_transition_requires_expected_status(client):
    lead_id = save()
    assert not db.transition(lead_id, "received", "needs_human", {"reason": "wrong claim"}, "Alert")
    lead = db.get_lead(lead_id)
    assert lead["status"] == "processing" and lead["reason"] is None
    assert lead["notify_text"] is None and lead["notify_generation"] == 0


def test_crash_after_shadow_transition_leaves_recoverable_alert(client, sent, monkeypatch):
    lead_id = save()
    db.update_lead(lead_id, reply="Stored draft", score="cold")
    clinic = clinics.load_clinic("demo_clinic")
    with monkeypatch.context() as patch:
        patch.setattr(router, "send_pending_alert", MagicMock(side_effect=RuntimeError("crash after commit")))
        with pytest.raises(RuntimeError, match="crash after commit"):
            router.route(db.get_lead(lead_id), clinic, score="cold", urgent=False,
                         needs_human=False, reply="Stored draft")
    lead = db.get_lead(lead_id)
    assert lead["status"] == "pending_approval" and lead["notify_text"]
    assert lead["notify_attempts"] == 0 and lead["owner_notified"] == 0
    recovery.run_recovery()
    assert db.get_lead(lead_id)["owner_notified"] == 1
    assert len(sent["telegram"]) == 1 and sent["email"] == []


@pytest.mark.parametrize("status,fields", [
    ("pending_approval", {}), ("needs_human", {}), ("delivery_unknown", {}),
    ("replied", {"score": "hot"}), ("replied", {"urgent": 1}),
])
def test_recovery_repairs_missing_alert(client, sent, status, fields):
    lead_id = save(status)
    db.update_lead(lead_id, reply="Stored draft", **fields)
    recovery.run_recovery()
    recovery.run_recovery()
    lead = db.get_lead(lead_id)
    assert lead["status"] == status
    assert lead["notify_text"] and "Stored draft" in lead["notify_text"]
    assert lead["notify_generation"] == 1 and lead["owner_notified"] == 1
    assert lead["notify_attempts"] == 1
    assert len(sent["telegram"]) == 1
    assert sent["email"] == []


def test_approval_rechecks_changed_prices_and_booking_link(client, sent, fake_llm, tmp_path, monkeypatch):
    clinic = clinics.load_clinic("demo_clinic")
    fake_llm({"score": "warm", "reply": f"Consultation is ₹300. Book: {clinic['booking_link']}"})
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    assert db.get_lead(lead_id)["status"] == "pending_approval"
    clinic.update(prices={"consultation": "₹900"}, booking_link="https://example.com/new-booking")
    (tmp_path / "demo_clinic.yaml").write_text(yaml.safe_dump(clinic), encoding="utf-8")
    monkeypatch.setenv("CLINICS_DIR", str(tmp_path))
    response = client.post(f"/admin/leads/{lead_id}/approve", headers={"X-Admin-Secret": "admin-secret"})
    assert response.status_code == 422
    assert any("amount" in reason for reason in response.json()["detail"])
    assert any("URL" in reason for reason in response.json()["detail"])
    lead = db.get_lead(lead_id)
    assert lead["status"] == "needs_human" and lead["needs_human"] == 1
    assert "amount" in lead["safety_flags"] and "URL" in lead["safety_flags"]
    assert lead["notify_generation"] == 2
    assert "Approval blocked" in sent["telegram"][-1][1]
    assert sent["email"] == []


@pytest.fixture
def smtp(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    monkeypatch.setenv("SMTP_USER", "test-user")
    monkeypatch.setenv("SMTP_PASSWORD", "test-password")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    factory = MagicMock()
    monkeypatch.setattr(mailer.smtplib, "SMTP", factory)
    return factory


@pytest.mark.parametrize("shutdown_method", ["quit", "close"])
def test_successful_smtp_send_ignores_shutdown_errors(smtp, caplog, shutdown_method):
    getattr(smtp.return_value, shutdown_method).side_effect = OSError("shutdown failed")
    assert mailer.send_email("patient@example.com", "Reply", "Thanks.") is mailer.DeliveryResult.SENT
    smtp.return_value.send_message.assert_called_once()
    smtp.return_value.quit.assert_called_once()
    smtp.return_value.close.assert_called_once()
    assert "failed" in caplog.text


@pytest.mark.parametrize("error", [TimeoutError("timeout"), OSError("dropped"),
                                       smtplib.SMTPServerDisconnected("disconnected")])
def test_exception_during_transmission_is_uncertain(smtp, error):
    smtp.return_value.send_message.side_effect = error
    assert mailer.send_email("patient@example.com", "Reply", "Thanks.") is mailer.DeliveryResult.UNCERTAIN


@pytest.mark.parametrize("stage", ["connect", "tls", "auth"])
def test_failure_before_transmission_is_failed(smtp, stage):
    if stage == "connect":
        smtp.side_effect = OSError("connect failed")
    elif stage == "tls":
        smtp.return_value.starttls.side_effect = OSError("TLS failed")
    else:
        smtp.return_value.login.side_effect = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    assert mailer.send_email("patient@example.com", "Reply", "Thanks.") is mailer.DeliveryResult.FAILED
    smtp.return_value.send_message.assert_not_called()


@pytest.mark.parametrize("error", [
    smtplib.SMTPRecipientsRefused({"patient@example.com": (550, b"refused")}),
    smtplib.SMTPSenderRefused(550, b"refused", "clinic@example.com"),
    smtplib.SMTPDataError(550, b"refused"),
])
def test_explicit_smtp_rejection_is_failed(smtp, error):
    smtp.return_value.send_message.side_effect = error
    assert mailer.send_email("patient@example.com", "Reply", "Thanks.") is mailer.DeliveryResult.FAILED


def test_uncertain_approval_is_not_sent_again(client, sent, fake_llm, monkeypatch):
    fake_llm({"score": "hot", "reply": "Our team can help you book a visit."})
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    sender = MagicMock(return_value=mailer.DeliveryResult.UNCERTAIN)
    monkeypatch.setattr(mailer, "send_email", sender)
    url, headers = f"/admin/leads/{lead_id}/approve", {"X-Admin-Secret": "admin-secret"}
    response = client.post(url, headers=headers)
    assert response.status_code == 502
    assert "Delivery to priya@example.com may or may not have happened" in response.json()["detail"]
    assert "check the sent folder before contacting the lead" in response.json()["detail"]
    assert client.post(url, headers=headers).status_code == 409
    recovery.run_recovery()
    sender.assert_called_once()
    lead = db.get_lead(lead_id)
    assert lead["status"] == "delivery_unknown" and lead["needs_human"] == 1
    assert lead["replied_at"] is None
    assert "may or may not have happened" in sent["telegram"][-1][1]


def test_uncertain_auto_reply_is_escalated(client, sent, fake_llm, auto_mode, monkeypatch):
    fake_llm({"score": "warm", "reply": "Our team can help you book a visit."})
    sender = MagicMock(return_value=mailer.DeliveryResult.UNCERTAIN)
    monkeypatch.setattr(mailer, "send_email", sender)
    lead_id = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()["lead_id"]
    lead = db.get_lead(lead_id)
    assert lead["status"] == "delivery_unknown" and lead["needs_human"] == 1
    assert lead["replied_at"] is None and lead["follow_up_at"] is None
    assert lead["notify_text"] and lead["owner_notified"] == 1
    sender.assert_called_once()


def test_uncertain_followup_remains_claimed(client, sent, monkeypatch):
    lead_id = save("replied")
    db.update_lead(lead_id, follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    sender = MagicMock(return_value=mailer.DeliveryResult.UNCERTAIN)
    monkeypatch.setattr(mailer, "send_email", sender)
    assert followup.run_followups() == 0
    assert followup.run_followups() == 0
    sender.assert_called_once()
    lead = db.get_lead(lead_id)
    assert lead["followup_sent"] == 1 and lead["followup_sent_at"] is None
    assert lead["status"] == "delivery_unknown" and lead["needs_human"] == 1
    assert "may or may not have happened" in sent["telegram"][-1][1]


@pytest.mark.parametrize("completion", [db.finish_alert, db.fail_alert])
@pytest.mark.parametrize("attempt", [1, 5])
def test_old_alert_generation_cannot_complete_replacement(client, completion, attempt):
    lead_id = save("pending_approval")
    def claim_at_attempt(text):
        db.queue_alert(lead_id, "owner", text)
        for _ in range(attempt - 1):
            claim = db.claim_alert(lead_id)
            db.fail_alert(lead_id, *claim)
        return db.claim_alert(lead_id)

    old_claim = claim_at_attempt("Old draft")
    new_claim = claim_at_attempt("Replacement draft")
    assert new_claim[0] == old_claim[0] + 1
    assert new_claim[1] == old_claim[1] == attempt
    assert completion(lead_id, *old_claim) is False
    lead = db.get_lead(lead_id)
    assert lead["owner_notified"] == 0 and lead["notify_text"] == "Replacement draft"
    assert lead["notify_claimed_at"] is not None
    assert lead["status"] == "pending_approval"
    assert db.finish_alert(lead_id, *new_claim)


def test_old_attempt_cannot_complete_current_attempt(client):
    lead_id = save("pending_approval")
    db.queue_alert(lead_id, "owner", "Draft")
    generation, first = db.claim_alert(lead_id)
    db.fail_alert(lead_id, generation, first)
    current = db.claim_alert(lead_id)
    assert current == (generation, first + 1)
    assert not db.finish_alert(lead_id, generation, first)
    assert db.get_lead(lead_id)["owner_notified"] == 0
    assert db.finish_alert(lead_id, *current)
