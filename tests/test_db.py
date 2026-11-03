from datetime import timedelta

from app import db


def _save(**kw):
    base = dict(clinic_id="demo_clinic", name="A", phone="+911234567890",
                email="a@example.com", message="hi", service=None)
    base.update(kw)
    return db.save_lead(**base)


def test_save_and_get(client):
    lead_id = _save()
    lead = db.get_lead(lead_id)
    assert lead["status"] == "received"
    assert lead["name"] == "A"


def test_duplicate_by_phone_and_email(client):
    first = _save()
    assert db.find_duplicate(clinic_id="demo_clinic", phone="+911234567890", email=None)["id"] == first
    assert db.find_duplicate(clinic_id="demo_clinic", phone="+910000000000", email="a@example.com")["id"] == first
    assert db.find_duplicate(clinic_id="demo_clinic", phone="+910000000000", email=None) is None
    assert db.find_duplicate(clinic_id="other_clinic", phone="+911234567890", email=None) is None


def test_duplicate_window_expires(client):
    _save()
    later = db.utcnow() + timedelta(hours=25)
    assert db.find_duplicate(clinic_id="demo_clinic", phone="+911234567890", email=None, now=later) is None


def test_update_rejects_unknown_field(client):
    lead_id = _save()
    try:
        db.update_lead(lead_id, id=99)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_due_followups(client):
    lead_id = _save()
    past = db.iso(db.utcnow() - timedelta(hours=1))
    db.update_lead(lead_id, status="replied", follow_up_at=past)
    assert [l["id"] for l in db.get_due_followups()] == [lead_id]
    db.update_lead(lead_id, followup_sent=1)
    assert db.get_due_followups() == []


def test_weekly_stats(client):
    lead_id = _save()
    db.update_lead(lead_id, score="hot", replied_at=db.iso(db.utcnow()))
    stats = db.weekly_stats()
    assert stats["leads_received"] == 1
    assert stats["by_score"] == {"hot": 1}
    assert stats["avg_response_seconds"] is not None


def test_atomic_dedup_by_email_clinic_and_window(client):
    def save(clinic_id="demo_clinic", phone="1234567", email="a@example.com"):
        return db.save_lead_dedup(clinic_id=clinic_id, name="A", phone=phone,
                                  email=email, message="hi", service=None)

    first, duplicate = save()
    assert duplicate is None
    second, duplicate = save(phone="7654321")
    assert duplicate == first
    assert db.get_lead(second)["is_duplicate"] == 1
    _, duplicate = save(clinic_id="other_clinic")
    assert duplicate is None
    with db.connect() as conn:
        conn.execute("UPDATE leads SET created_at=? WHERE clinic_id=?",
                     (db.iso(db.utcnow() - timedelta(hours=25)), "demo_clinic"))
    _, duplicate = save()
    assert duplicate is None
