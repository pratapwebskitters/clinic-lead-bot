from app import db
from tests.conftest import HEADERS, lead_payload


def test_rejects_missing_secret(client):
    assert client.post("/webhook/lead", json=lead_payload()).status_code == 401


def test_rejects_wrong_secret(client):
    r = client.post("/webhook/lead", json=lead_payload(), headers={"X-Webhook-Secret": "nope"})
    assert r.status_code == 401


def test_rejects_invalid_payload(client):
    r = client.post("/webhook/lead", json=lead_payload(phone="12"), headers=HEADERS)
    assert r.status_code == 422
    r = client.post("/webhook/lead", json=lead_payload(email="not-an-email"), headers=HEADERS)
    assert r.status_code == 422


def test_rejects_path_traversal_clinic_id(client):
    r = client.post("/webhook/lead", json=lead_payload(clinic_id="../secrets"), headers=HEADERS)
    assert r.status_code == 422


def test_saves_lead_and_normalizes_phone(client, sent, fake_llm):
    fake_llm({"score": "cold", "reply": "Thanks! Book here: https://cal.com/REPLACE/consultation"})
    r = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "received"
    assert db.get_lead(body["lead_id"])["phone"] == "+919800000000"


def test_duplicate_is_flagged_and_not_reprocessed(client, sent, fake_llm):
    calls = fake_llm({"score": "cold", "reply": "Thanks, our team will contact you."})
    first = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()
    second = client.post("/webhook/lead", json=lead_payload(), headers=HEADERS).json()
    assert second["status"] == "duplicate"
    assert second["duplicate_of"] == first["lead_id"]
    assert calls["n"] == 1  # LLM called only for the first lead
