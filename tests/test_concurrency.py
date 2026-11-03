import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest

from app import db, pipeline
from app.config import get_settings
from app.services import followup, mailer
from tests.conftest import HEADERS, lead_payload


def save_lead(**overrides):
    payload = lead_payload()
    payload.update(phone="+919800000000", service=None)
    payload.update(overrides)
    return db.save_lead(**payload)


def test_ten_simultaneous_webhooks_deduplicate(client, sent, fake_llm):
    calls = fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    barrier = Barrier(10)

    def post(index):
        barrier.wait(timeout=10)
        response = client.post("/webhook/lead",
                               json=lead_payload(email=f"patient{index}@example.com"),
                               headers=HEADERS)
        assert response.status_code == 200
        return response.json()

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(post, range(10)))
    originals = [r for r in results if r["status"] == "received"]
    duplicates = [r for r in results if r["status"] == "duplicate"]
    assert len(originals) == 1
    assert len(duplicates) == 9
    assert {r["duplicate_of"] for r in duplicates} == {originals[0]["lead_id"]}
    rows = [db.get_lead(r["lead_id"]) for r in results]
    assert sum(not row["is_duplicate"] for row in rows) == 1
    assert len({row["id"] for row in rows}) == 10
    assert calls["n"] == 1


def test_processing_claim_prevents_repeat_work(client, sent, fake_llm, auto_mode):
    calls = fake_llm({"score": "cold", "reply": "Thanks for contacting us."})
    lead_id = save_lead()
    barrier = Barrier(2)

    def process(_):
        barrier.wait(timeout=10)
        pipeline.process_lead(lead_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(process, range(2)))
    pipeline.process_lead(lead_id)
    assert calls["n"] == 1
    assert len(sent["email"]) == 1
    assert db.get_lead(lead_id)["status"] == "replied"


def test_processing_read_error_is_recorded_and_alerted(client, sent, monkeypatch):
    lead_id = save_lead()
    original_get = db.get_lead

    def fail_read(_):
        raise sqlite3.OperationalError("read failed")

    monkeypatch.setattr(db, "get_lead", fail_read)
    pipeline.process_lead(lead_id)
    lead = original_get(lead_id)
    assert lead["status"] == "needs_human"
    assert lead["needs_human"] == 1
    assert sent["telegram"]
    assert f"#{lead_id}" in sent["telegram"][0][1]


def test_two_approvals_send_one_email(client, sent, monkeypatch):
    lead_id = save_lead(status="pending_approval")
    db.update_lead(lead_id, reply="Thanks for contacting us.", score="cold")
    barrier = Barrier(2)
    original_get = db.get_lead

    def read_together(requested_id):
        lead = original_get(requested_id)
        barrier.wait(timeout=10)
        return lead

    monkeypatch.setattr(db, "get_lead", read_together)

    def approve(_):
        return client.post(f"/admin/leads/{lead_id}/approve",
                           headers={"X-Admin-Secret": "admin-secret"}).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = list(pool.map(approve, range(2)))
    assert sorted(statuses) == [200, 409]
    assert len(sent["email"]) == 1
    assert original_get(lead_id)["status"] == "replied"


@pytest.mark.parametrize("raises", [False, True])
def test_failed_approval_releases_claim(client, sent, monkeypatch, raises):
    lead_id = save_lead(status="pending_approval")
    db.update_lead(lead_id, reply="Thanks for contacting us.")
    original_send = mailer.send_email

    def fail_send(**kwargs):
        assert db.get_lead(lead_id)["status"] == "sending"
        if raises:
            raise RuntimeError("SMTP failed")
        return mailer.DeliveryResult.FAILED

    monkeypatch.setattr(mailer, "send_email", fail_send)
    url = f"/admin/leads/{lead_id}/approve"
    headers = {"X-Admin-Secret": "admin-secret"}
    assert client.post(url, headers=headers).status_code == 502
    assert db.get_lead(lead_id)["status"] == "pending_approval"
    monkeypatch.setattr(mailer, "send_email", original_send)
    assert client.post(url, headers=headers).status_code == 200
    assert len(sent["email"]) == 1


def test_concurrent_followup_runners_send_once(client, sent, monkeypatch):
    ids = [save_lead(phone=str(1234567 + n), email=f"patient{n}@example.com")
           for n in range(3)]
    for lead_id in ids:
        db.update_lead(lead_id, status="replied", score="warm",
                       follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    barrier = Barrier(2)
    original_due = db.get_due_followups

    def due_together(now=None):
        leads = original_due(now)
        barrier.wait(timeout=10)
        return leads

    monkeypatch.setattr(db, "get_due_followups", due_together)
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(pool.map(lambda _: followup.run_followups(), range(2)))
    assert sum(counts) == 3
    assert len(sent["email"]) == 3
    assert {email["to"] for email in sent["email"]} == {
        f"patient{n}@example.com" for n in range(3)
    }
    assert all(db.get_lead(lead_id)["followup_sent"] == 1 for lead_id in ids)
    assert all(db.get_lead(lead_id)["followup_sent_at"] for lead_id in ids)


@pytest.mark.parametrize("raises", [False, True])
def test_failed_followup_is_not_retried(client, sent, monkeypatch, caplog, raises):
    lead_id = save_lead()
    db.update_lead(lead_id, status="replied",
                   follow_up_at=db.iso(db.utcnow() - timedelta(hours=1)))
    attempts = []

    def fail_send(**kwargs):
        assert db.get_lead(lead_id)["followup_sent"] == 1
        attempts.append(kwargs)
        if raises:
            raise RuntimeError("SMTP failed")
        return mailer.DeliveryResult.FAILED

    monkeypatch.setattr(mailer, "send_email", fail_send)
    assert followup.run_followups() == 0
    assert followup.run_followups() == 0
    assert len(attempts) == 1
    assert db.get_lead(lead_id)["followup_sent_at"] is None
    assert "follow-up" in caplog.text


def test_migration_preserves_legacy_rows_and_is_idempotent():
    with sqlite3.connect(get_settings().db_path) as conn:
        conn.execute("CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT)")
        conn.execute("INSERT INTO leads (name) VALUES ('Existing patient')")
    db.init_db()
    db.init_db()
    lead = db.get_lead(1)
    assert lead["name"] == "Existing patient"
    assert lead["status"] == "received"
    assert lead["followup_sent"] == 0
    assert set(db.MIGRATION_COLUMNS) <= lead.keys()
    with db.connect() as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 10000
    assert save_lead() == 2
