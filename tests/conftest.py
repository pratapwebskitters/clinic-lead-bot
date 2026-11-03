import json

import pytest

from app import clinics
from app.services import mailer


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("ADMIN_SECRET", "admin-secret")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setenv("ENABLE_SCHEDULER", "0")
    monkeypatch.setenv("ENABLE_RECOVERY", "0")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setenv("ADMIN_TELEGRAM_CHAT_ID", "admin-chat")
    for var in ("SMTP_HOST", "SMTP_FROM"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def sent(monkeypatch):
    """Capture outgoing Telegram messages and emails instead of sending them."""
    records = {"telegram": [], "email": []}

    def fake_telegram(chat_id, text, deadline=None):
        records["telegram"].append((chat_id, text))
        return True

    def fake_email(to, subject, body, from_name=None, deadline=None):
        records["email"].append({"to": to, "subject": subject, "body": body})
        return mailer.DeliveryResult.SENT

    monkeypatch.setattr("app.services.notify.send_telegram", fake_telegram)
    monkeypatch.setattr("app.services.mailer.send_email", fake_email)
    return records


@pytest.fixture
def fake_llm(monkeypatch):
    """Call fake_llm(dict_or_str_or_list) to script the provider's raw output."""
    def _set(output):
        outputs = list(output) if isinstance(output, list) else [output]
        calls = {"n": 0}

        def fake_call(system, user, timeout=None, deadline=None):
            item = outputs[min(calls["n"], len(outputs) - 1)]
            calls["n"] += 1
            if isinstance(item, Exception):
                raise item
            return item if isinstance(item, str) else json.dumps(item)

        monkeypatch.setattr("app.services.llm.call_provider", fake_call)
        return calls

    return _set


@pytest.fixture
def auto_mode(monkeypatch):
    """Make the demo clinic reply directly to leads instead of using shadow mode."""
    cfg = dict(clinics.load_clinic("demo_clinic"), mode="auto")
    monkeypatch.setattr("app.clinics.load_clinic", lambda clinic_id: cfg)
    return cfg


def lead_payload(**overrides):
    base = {
        "name": "Priya", "phone": "+91 98000 00000", "email": "priya@example.com",
        "message": "Want a root canal appointment tomorrow", "clinic_id": "demo_clinic",
    }
    base.update(overrides)
    return base


HEADERS = {"X-Webhook-Secret": "test-secret"}
