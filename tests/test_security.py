import logging
import ssl
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app import clinics, db
from app.main import _require_secret
from app.models import LeadIn
from app.services import mailer, notify
from tests.conftest import HEADERS, lead_payload


@pytest.mark.parametrize("outcome", [200, 403, "timeout", "unexpected"])
def test_telegram_logs_never_contain_token(monkeypatch, caplog, outcome):
    token = "test-bot-token-do-not-log"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    caplog.set_level(logging.DEBUG)
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING

    def respond(request):
        if outcome == "timeout":
            raise httpx.ReadTimeout(f"Failed URL: {request.url}", request=request)
        if outcome == "unexpected":
            raise RuntimeError(f"Failed URL: {request.url}")
        return httpx.Response(outcome, json={"ok": outcome == 200})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(notify.httpx, "stream", client.stream)
        assert notify.send_telegram("owner-chat", "Test alert") is (outcome == 200)

    assert token not in caplog.text
    for record in caplog.records:
        assert token not in repr(record.__dict__)
    if outcome == 403:
        assert "HTTPStatusError" in caplog.text and "403" in caplog.text
    elif outcome == "timeout":
        assert "ReadTimeout" in caplog.text
    elif outcome == "unexpected":
        assert "RuntimeError" in caplog.text


def test_smtp_tls_verifies_certificate_and_hostname(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_FROM", "clinic@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "1")
    smtp = MagicMock()
    monkeypatch.setattr(mailer.smtplib, "SMTP", smtp)

    assert mailer.send_email("patient@example.com", "Enquiry", "Thanks!") is mailer.DeliveryResult.SENT
    server = smtp.return_value
    server.starttls.assert_called_once()
    context = server.starttls.call_args.kwargs["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    server.send_message.assert_called_once()


@pytest.mark.parametrize("email", [
    "victim@example.com,attacker", "a@b.com;c@d.com", "a;b@example.com",
    "<a@b.com>", "a<b@example.com", "a>b@example.com", "a@@b.com",
    " a@b.com", "a@b.com ", "a @b.com", "a\t@b.com", "a@b.com\n",
])
def test_email_rejects_recipient_injection_and_whitespace(email):
    with pytest.raises(ValidationError):
        LeadIn(**lead_payload(email=email))


def test_email_preserves_optional_and_single_address_behavior():
    assert LeadIn(**lead_payload(email=None)).email is None
    assert LeadIn(**lead_payload(email="")).email is None
    assert LeadIn(**lead_payload(email="Patient@Example.COM")).email == "patient@example.com"


def test_clinic_id_is_normalized_in_model_and_loader():
    assert LeadIn(**lead_payload(clinic_id="DEMO_CLINIC")).clinic_id == "demo_clinic"
    assert clinics.load_clinic("DEMO_CLINIC") == clinics.load_clinic("demo_clinic")


def test_clinic_case_variants_share_duplicate_detection(client, sent, fake_llm):
    calls = fake_llm({"score": "cold", "reply": "Our team will contact you."})
    first = client.post("/webhook/lead", json=lead_payload(clinic_id="DEMO_CLINIC"),
                        headers=HEADERS).json()
    second = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()
    assert db.get_lead(first["lead_id"])["clinic_id"] == "demo_clinic"
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == first["lead_id"]
    assert calls["n"] == 1


@pytest.mark.parametrize("provided,expected", [
    ("wrong-é", "test-secret"), ("wrong", "secret-é"), ("错误", "密码"),
])
def test_unicode_secret_mismatch_is_unauthorized(provided, expected):
    with pytest.raises(HTTPException) as exc:
        _require_secret(provided, expected)
    assert exc.value.status_code == 401


def test_matching_unicode_secret_is_supported():
    _require_secret("secret-é", "secret-é")


def test_non_ascii_header_returns_401(client):
    response = client.post("/webhook/lead", json=lead_payload(),
                           headers={"X-Webhook-Secret": b"wrong-\xc3\xa9"})
    assert response.status_code == 401
