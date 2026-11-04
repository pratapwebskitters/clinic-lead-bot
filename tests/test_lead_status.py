"""Read-only, authenticated processing snapshots for CRM integrations."""
import pytest

from app import db
from tests.conftest import lead_payload

AUTH = {"X-Admin-Secret": "admin-secret"}


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Secret": "wrong"},
                                     {"X-Webhook-Secret": "test-secret"}])
def test_lead_status_requires_admin_secret_before_lookup(client, headers, monkeypatch):
    def forbidden_lookup(*_):
        pytest.fail("unauthenticated request accessed lead data")
    monkeypatch.setattr(db, "get_lead", forbidden_lookup)
    assert client.get('/admin/leads/1', headers=headers).status_code == 401


def test_lead_status_missing_lead_returns_404(client):
    assert client.get('/admin/leads/999', headers=AUTH).status_code == 404


@pytest.mark.parametrize("status,human,review", [
    ("received", False, False), ("processing", False, False), ("sending", False, False),
    ("pending_approval", False, True), ("replied", False, False),
    ("replied", True, True), ("needs_human", True, True), ("delivery_unknown", True, True),
    ("spam", False, False),
])
def test_lead_status_is_truthful_and_has_no_delivery_side_effects(client, sent, status, human, review):
    lead_id = db.save_lead(**lead_payload(service=None), status=status)
    db.update_lead(lead_id, score="hot", reply="Our team can help.", needs_human=int(human),
                   claim_token="worker-token-private", notify_chat_id="private-owner-chat")
    db.queue_alert(lead_id, "private-owner-chat", "private-owner-notification")
    before = db.get_lead(lead_id)
    for _ in range(2):
        response = client.get(f'/admin/leads/{lead_id}', headers=AUTH)
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        body = response.json()
        assert body["lead_id"] == lead_id and body["status"] == status
        assert body["score"] == "hot" and body["reply"] == "Our team can help."
        assert body["needs_human"] is human and body["review_required"] is review
        assert body["sent_at"] is None
        assert set(body) == {"lead_id", "clinic_id", "status", "score", "intent", "reason",
                             "reply", "safety_flags", "duplicate_of", "created_at", "replied_at",
                             "sent_at", "followup_status", "urgent", "needs_human", "review_required"}
        assert "private" not in response.text
    assert db.get_lead(lead_id) == before
    assert sent == {"email": [], "telegram": []}


def test_status_preserves_confirmed_send_evidence_during_escalation(client):
    lead_id = db.save_lead(**lead_payload(service=None), status="delivery_unknown")
    timestamp = db.iso(db.utcnow())
    db.update_lead(lead_id, urgent=1, sent_at=timestamp, replied_at=timestamp,
                   followup_status="uncertain")
    body = client.get(f'/admin/leads/{lead_id}', headers=AUTH).json()
    assert body["status"] == "delivery_unknown" and body["review_required"]
    assert body["sent_at"] == timestamp and body["urgent"] is True
    assert body["followup_status"] == "uncertain"


def test_duplicate_status_links_original_without_fabricating_qualification(client):
    first, _ = db.save_lead_dedup(**lead_payload(service=None))
    duplicate, original = db.save_lead_dedup(**lead_payload(service=None))
    body = client.get(f'/admin/leads/{duplicate}', headers=AUTH).json()
    assert original == first and body["duplicate_of"] == first
    assert body["status"] == "duplicate" and body["score"] is None
