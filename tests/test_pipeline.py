from app import db
from tests.conftest import HEADERS, lead_payload

BOOK = "https://cal.com/REPLACE/consultation"


def _llm(**kw):
    base = {"language": "en", "intent": "booking", "score": "hot", "urgent": False,
            "needs_human": False, "reason": "test", "reply": f"Happy to help! Book here: {BOOK}"}
    base.update(kw)
    return base


def _post(client, **overrides):
    r = client.post("/webhook/lead", json=lead_payload(**overrides), headers=HEADERS)
    assert r.status_code == 200
    return db.get_lead(r.json()["lead_id"])


def test_hot_lead_auto_mode(client, sent, fake_llm, auto_mode):
    fake_llm(_llm())
    lead = _post(client)
    assert lead["status"] == "replied"
    assert lead["replied_at"] is not None
    assert sent["email"][0]["to"] == "priya@example.com"
    assert len(sent["telegram"]) == 1  # hot lead alerts the owner


def test_warm_lead_schedules_followup(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(score="warm"))
    lead = _post(client)
    assert lead["status"] == "replied"
    assert lead["follow_up_at"] is not None
    assert sent["telegram"] == []


def test_cold_lead_replied_without_alert(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(score="cold"))
    lead = _post(client)
    assert lead["status"] == "replied"
    assert lead["follow_up_at"] is None
    assert sent["telegram"] == []


def test_spam_gets_no_reply(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(score="spam", reply="should never be sent"))
    lead = _post(client, message="Buy cheap followers")
    assert lead["status"] == "spam"
    assert sent["email"] == [] and sent["telegram"] == []


def test_urgent_reaches_human(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(urgent=True, needs_human=False))
    lead = _post(client, message="Severe tooth pain")
    assert lead["urgent"] == 1 and lead["needs_human"] == 1
    assert sent["telegram"] and "URGENT" in sent["telegram"][0][1]


def test_llm_failure_sends_safe_template_and_alerts(client, sent, fake_llm, auto_mode):
    fake_llm(RuntimeError("API down"))
    lead = _post(client)
    assert lead["status"] == "needs_human"
    assert lead["needs_human"] == 1
    assert BOOK in sent["email"][0]["body"]  # safe template contains the booking link
    assert sent["telegram"]


def test_safety_rejection_replaces_reply(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(reply="Root canal costs only ₹9999, guaranteed!"))
    lead = _post(client)
    assert lead["safety_flags"]
    assert "9999" not in sent["email"][0]["body"]
    assert lead["needs_human"] == 1


def test_no_email_means_human_alert(client, sent, fake_llm, auto_mode):
    fake_llm(_llm(score="cold"))
    lead = _post(client, email=None)
    assert lead["status"] == "needs_human"
    assert lead["needs_human"] == 1
    assert sent["email"] == []
    assert sent["telegram"]


def test_shadow_mode_sends_draft_to_owner_only(client, sent, fake_llm):
    fake_llm(_llm())
    lead = _post(client)
    assert lead["status"] == "pending_approval"
    assert sent["email"] == []
    assert "SHADOW MODE" in sent["telegram"][0][1]


def test_approve_endpoint_sends_draft(client, sent, fake_llm):
    fake_llm(_llm())
    lead = _post(client)
    r = client.post(f"/admin/leads/{lead['id']}/approve", headers={"X-Admin-Secret": "admin-secret"})
    assert r.status_code == 200
    assert sent["email"][0]["to"] == "priya@example.com"
    assert db.get_lead(lead["id"])["status"] == "replied"
    again = client.post(f"/admin/leads/{lead['id']}/approve", headers={"X-Admin-Secret": "admin-secret"})
    assert again.status_code == 409


def test_approve_requires_admin_secret(client):
    assert client.post("/admin/leads/1/approve").status_code == 401
    assert client.post("/admin/leads/1/approve", headers={"X-Admin-Secret": "bad"}).status_code == 401


def test_unknown_clinic_keeps_lead_and_alerts_admin(client, sent, fake_llm):
    fake_llm(_llm())
    lead = _post(client, clinic_id="does_not_exist")
    assert lead["status"] == "needs_human"
    assert sent["telegram"][0][0] == "admin-chat"


def test_followup_job_sends_once(client, sent, fake_llm, auto_mode):
    from datetime import timedelta

    from app.services import followup

    fake_llm(_llm(score="warm"))
    lead = _post(client)
    later = db.utcnow() + timedelta(hours=25)
    assert followup.run_followups(now=later) == 1
    assert followup.run_followups(now=later) == 0
    assert len(sent["email"]) == 2


def test_unsafe_fallback_is_not_sent(client, sent, fake_llm, auto_mode):
    from app.services import safety

    auto_mode["clinic_name"] = "Guaranteed painless cure clinic"
    assert not safety.check_reply(safety.safe_reply(auto_mode), auto_mode)[0]
    fake_llm(_llm(reply="Costs ₹9999"))
    lead = _post(client)
    assert lead["status"] == "needs_human"
    assert lead["needs_human"] == 1
    assert lead["reply"] is None
    assert lead["safety_flags"]
    assert sent["email"] == []
    assert sent["telegram"]


def test_followup_checks_safety_before_sending(client, sent, fake_llm, auto_mode, caplog):
    from datetime import timedelta

    from app.services import followup

    fake_llm(_llm(score="warm"))
    lead = _post(client)
    auto_mode["clinic_name"] = "Painless clinic"
    assert followup.run_followups(now=db.utcnow() + timedelta(hours=25)) == 0
    assert len(sent["email"]) == 1
    assert db.get_lead(lead["id"])["followup_sent"] == 1
    assert followup.run_followups(now=db.utcnow() + timedelta(hours=25)) == 0
    assert len(sent["email"]) == 1
    assert "follow-up" in caplog.text and "rejected" in caplog.text


def test_followup_sanitizes_and_truncates_name():
    from app.services import followup

    text = followup.followup_text({"name": "A\n\r\t\x00\x7f\u202e" + "B" * 70}, {})
    assert text.startswith("Hi A" + "B" * 49 + ",")
    assert not any(c in text for c in "\n\r\t\x00\x7f\u202e")
