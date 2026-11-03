"""SQLite access layer. One short-lived connection per call (thread-safe)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterator, Optional, TypeVar
from uuid import uuid4

from app.config import get_settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clinic_id TEXT NOT NULL,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    email TEXT,
    message TEXT NOT NULL,
    service TEXT,
    status TEXT NOT NULL DEFAULT 'received',
    is_duplicate INTEGER NOT NULL DEFAULT 0,
    duplicate_of INTEGER,
    repeat_note_needed INTEGER NOT NULL DEFAULT 0,
    processing_backlog INTEGER NOT NULL DEFAULT 0,
    worker_abandoned INTEGER NOT NULL DEFAULT 0,
    claim_token TEXT,
    score TEXT,
    intent TEXT,
    language TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    needs_human INTEGER NOT NULL DEFAULT 0,
    reason TEXT,
    reply TEXT,
    safety_flags TEXT,
    created_at TEXT NOT NULL,
    replied_at TEXT,
    sent_at TEXT,
    follow_up_at TEXT,
    followup_sent INTEGER NOT NULL DEFAULT 0,
    followup_sent_at TEXT,
    followup_status TEXT,
    followup_claimed_at TEXT,
    booked INTEGER NOT NULL DEFAULT 0,
    status_updated_at TEXT,
    owner_notified INTEGER NOT NULL DEFAULT 0,
    notify_attempts INTEGER NOT NULL DEFAULT 0,
    notify_generation INTEGER NOT NULL DEFAULT 0,
    notify_chat_id TEXT,
    notify_text TEXT,
    notify_claim_token TEXT,
    notify_claimed_at TEXT
);
"""

# Defaults allow older rows to survive the addition of required columns.
MIGRATION_COLUMNS = {
    "clinic_id": "TEXT NOT NULL DEFAULT 'demo_clinic'",
    "name": "TEXT NOT NULL DEFAULT ''", "phone": "TEXT NOT NULL DEFAULT ''",
    "email": "TEXT", "message": "TEXT NOT NULL DEFAULT ''", "service": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'received'",
    "is_duplicate": "INTEGER NOT NULL DEFAULT 0", "score": "TEXT", "intent": "TEXT",
    "duplicate_of": "INTEGER", "repeat_note_needed": "INTEGER NOT NULL DEFAULT 0",
    "processing_backlog": "INTEGER NOT NULL DEFAULT 0",
    "worker_abandoned": "INTEGER NOT NULL DEFAULT 0",
    "claim_token": "TEXT", "sent_at": "TEXT", "notify_claim_token": "TEXT",
    "language": "TEXT", "urgent": "INTEGER NOT NULL DEFAULT 0",
    "needs_human": "INTEGER NOT NULL DEFAULT 0", "reason": "TEXT", "reply": "TEXT",
    "safety_flags": "TEXT", "created_at": "TEXT NOT NULL DEFAULT ''",
    "replied_at": "TEXT", "follow_up_at": "TEXT",
    "followup_sent": "INTEGER NOT NULL DEFAULT 0", "followup_sent_at": "TEXT",
    "followup_status": "TEXT", "followup_claimed_at": "TEXT",
    "booked": "INTEGER NOT NULL DEFAULT 0",
    "status_updated_at": "TEXT",
    "owner_notified": "INTEGER NOT NULL DEFAULT 0",
    "notify_attempts": "INTEGER NOT NULL DEFAULT 0",
    "notify_generation": "INTEGER NOT NULL DEFAULT 0",
    "notify_chat_id": "TEXT", "notify_text": "TEXT", "notify_claimed_at": "TEXT",
}

UPDATABLE = {
    "status", "score", "intent", "language", "urgent", "needs_human", "reason", "reply",
    "safety_flags", "replied_at", "follow_up_at", "followup_sent", "followup_sent_at", "booked",
    "status_updated_at", "owner_notified", "notify_attempts",
    "notify_chat_id",
    "followup_status", "followup_claimed_at",
    "processing_backlog", "worker_abandoned",
    "claim_token", "sent_at", "notify_claim_token",
}
DRAFT_FENCE = ("(notify_claim_token IS NULL OR (COALESCE(notify_claim_token=claim_token,0)"
               " AND status='pending_approval' AND worker_abandoned=0))")


_Result = TypeVar("_Result")


def fault_hook(name: str) -> None:
    """Test-only crash injection; intentionally a no-op in production."""


def external_call(name: str, function: Callable[..., _Result], *args: Any, **kwargs: Any) -> _Result:
    """Expose the two sides of an external side effect, including failed calls."""
    fault_hook(f"before_send:{name}")
    try:
        return function(*args, **kwargs)
    finally:
        fault_hook(f"after_send:{name}")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(get_settings().db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        yield conn
        fault_hook("before_commit")
        conn.commit()
        fault_hook("after_commit")
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        fault_hook("before_schema_commit")
        conn.executescript(SCHEMA)
        fault_hook("after_schema_commit")
        conn.execute("BEGIN IMMEDIATE")
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(leads)")}
        for name, definition in MIGRATION_COLUMNS.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE leads ADD COLUMN {name} {definition}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_phone ON leads(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_leads_created ON leads(created_at)")
        conn.execute("UPDATE leads SET status_updated_at=created_at WHERE status_updated_at IS NULL")
        conn.execute("UPDATE leads SET needs_human=1 WHERE status='needs_human'")
        conn.execute("UPDATE leads SET followup_status='sent'"
                     " WHERE followup_status IS NULL AND followup_sent_at IS NOT NULL")


def save_lead(
    *, clinic_id: str, name: str, phone: str, email: Optional[str], message: str,
    service: Optional[str], status: str = "received", is_duplicate: bool = False,
) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO leads (clinic_id, name, phone, email, message, service, status,"
            " is_duplicate, created_at, needs_human) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (clinic_id, name, phone, email, message, service, status,
             int(is_duplicate), iso(utcnow()), int(status == "needs_human")),
        )
        return int(cur.lastrowid)


def get_lead(lead_id: int) -> Optional[dict[str, Any]]:
    with connect() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    return dict(row) if row else None


def lead_status(lead_id: int) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
    return row["status"] if row else None


def save_lead_dedup(
    *, clinic_id: str, name: str, phone: str, email: Optional[str], message: str,
    service: Optional[str],
) -> tuple[int, Optional[int]]:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        now = utcnow()
        cutoff = iso(now - timedelta(hours=get_settings().duplicate_window_hours))
        duplicate = _find_duplicate(conn, clinic_id, phone, email, cutoff)
        duplicate_id = duplicate["id"] if duplicate else None
        repeat_note = bool(duplicate and message != duplicate["message"] and not conn.execute(
            "SELECT id FROM leads WHERE duplicate_of=? AND message=? AND repeat_note_needed=1 LIMIT 1",
            (duplicate_id, message),
        ).fetchone())
        cur = conn.execute(
            "INSERT INTO leads (clinic_id, name, phone, email, message, service, status,"
            " is_duplicate, created_at, duplicate_of, repeat_note_needed) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (clinic_id, name, phone, email, message, service,
             "duplicate" if duplicate_id is not None else "received",
             int(duplicate_id is not None), iso(now), duplicate_id, int(repeat_note)),
        )
        return int(cur.lastrowid), duplicate_id


def _find_duplicate(conn: sqlite3.Connection, clinic_id: str, phone: str,
                    email: Optional[str], cutoff: str) -> Optional[dict[str, Any]]:
    row = conn.execute(
        "SELECT * FROM leads WHERE clinic_id=? AND created_at>=?"
        " AND (phone=? OR (?<>'' AND email=?)) ORDER BY id DESC LIMIT 1",
        (clinic_id, cutoff, phone, email or "", email or ""),
    ).fetchone()
    if row is None:
        return None
    if not row["is_duplicate"]:
        return dict(row)
    if row["duplicate_of"] is not None:
        original = conn.execute("SELECT * FROM leads WHERE id=? AND is_duplicate=0",
                                (row["duplicate_of"],)).fetchone()
    else:
        # Resolve duplicate rows written before duplicate_of was introduced.
        original = conn.execute(
            "SELECT * FROM leads WHERE clinic_id=? AND is_duplicate=0 AND id<?"
            " AND (phone=? OR (?<>'' AND email=?)) ORDER BY id DESC LIMIT 1",
            (clinic_id, row["id"], row["phone"], row["email"] or "", row["email"] or ""),
        ).fetchone()
    return dict(original) if original else None


def claim_status(lead_id: int, expected: str, status: str) -> bool:
    if status == "processing":
        return claim_processing(lead_id, expected) is not None
    with connect() as conn:
        return conn.execute(
            "UPDATE leads SET status=?, status_updated_at=?,"
            " processing_backlog=CASE WHEN ?='processing' THEN 0 ELSE processing_backlog END,"
            " needs_human=CASE WHEN ?='needs_human' THEN 1 ELSE needs_human END"
            " WHERE id=? AND status=?",
            (status, iso(utcnow()), status, status, lead_id, expected),
        ).rowcount == 1


def claim_processing(lead_id: int, expected_status: str = "received") -> Optional[str]:
    token = str(uuid4())
    with connect() as conn:
        changed = conn.execute(
            "UPDATE leads SET status='processing', status_updated_at=?, claim_token=?, processing_backlog=0"
            " WHERE id=? AND status=? AND worker_abandoned=0",
            (iso(utcnow()), token, lead_id, expected_status),
        ).rowcount
    return token if changed else None


def begin_send(lead_id: int, token: str) -> bool:
    if not token:
        return False
    with connect() as conn:
        return conn.execute(
            "UPDATE leads SET status='sending', status_updated_at=?"
            " WHERE id=? AND status='processing' AND claim_token=? AND worker_abandoned=0",
            (iso(utcnow()), lead_id, token),
        ).rowcount == 1


def claim_followup(lead_id: int) -> Optional[str]:
    token = str(uuid4())
    with connect() as conn:
        changed = conn.execute(
            "UPDATE leads SET followup_sent=1, followup_status='claimed', followup_claimed_at=?,"
            " status='processing', status_updated_at=?, claim_token=?"
            " WHERE id=? AND status='replied' AND followup_sent=0 AND booked=0 AND worker_abandoned=0",
            (iso(utcnow()), iso(utcnow()), token, lead_id),
        ).rowcount
    return token if changed else None


def find_duplicate(
    *, clinic_id: str, phone: str, email: Optional[str],
    window_hours: Optional[int] = None, now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    hours = window_hours if window_hours is not None else get_settings().duplicate_window_hours
    cutoff = iso((now or utcnow()) - timedelta(hours=hours))
    with connect() as conn:
        return _find_duplicate(conn, clinic_id, phone, email, cutoff)


def update_lead(lead_id: int, **fields: Any) -> None:
    if "status" in fields:
        fields.setdefault("status_updated_at", iso(utcnow()))
        if fields["status"] == "needs_human":
            fields["needs_human"] = 1
    bad = set(fields) - UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE leads SET {assignments} WHERE id=?", (*fields.values(), lead_id))


def update_claimed(lead_id: int, token: str, fields: dict[str, Any], status: str = "processing") -> bool:
    if not token:
        return False
    bad = set(fields) - UPDATABLE
    if bad or "status" in fields or "claim_token" in fields:
        raise ValueError("invalid claimed update fields")
    assignments = ", ".join(f"{key}=?" for key in fields)
    with connect() as conn:
        return conn.execute(
            f"UPDATE leads SET {assignments} WHERE id=? AND status=? AND claim_token=? AND worker_abandoned=0",
            (*fields.values(), lead_id, status, token),
        ).rowcount == 1


def _transition(conn: sqlite3.Connection, lead_id: int, expected_status: str,
                new_status: str, fields: dict[str, Any], alert_text: Optional[str],
                claim_token: Optional[str] = None) -> bool:
    values = dict(fields, status=new_status, status_updated_at=iso(utcnow()))
    if new_status in {"needs_human", "delivery_unknown"}:
        values["needs_human"] = 1
    if new_status in {"needs_human", "delivery_unknown", "replied", "spam"}:
        values["claim_token"] = None
    bad = set(values) - UPDATABLE
    if bad:
        raise ValueError(f"cannot update fields: {sorted(bad)}")
    assignments = ", ".join(f"{name}=?" for name in values)
    fence = " AND claim_token=? AND worker_abandoned=0" if claim_token is not None else ""
    changed = conn.execute(
        f"UPDATE leads SET {assignments} WHERE id=? AND status=?{fence}",
        (*values.values(), lead_id, expected_status, *((claim_token,) if claim_token is not None else ())),
    ).rowcount
    if not changed:
        return False
    if alert_text is not None:
        chat_id = fields.get("notify_chat_id", get_settings().admin_telegram_chat_id)
        _queue_alert(conn, lead_id, chat_id, alert_text, fields.get("notify_claim_token"))
    return True


def transition(lead_id: int, expected_status: str, new_status: str,
               fields: dict[str, Any], alert_text: Optional[str] = None,
               claim_token: Optional[str] = None) -> bool:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _transition(conn, lead_id, expected_status, new_status, fields, alert_text, claim_token)


def record_send(lead_id: int, token: str, outcome: str, timestamp: str, fields: dict[str, Any],
                chat_id: str, alert_text: Optional[str], late_alert_text: str,
                success_status: str = "replied", followup: bool = False,
                failed_status: str = "needs_human") -> tuple[Optional[str], bool]:
    """Confirmed delivery evidence survives escalation; all other worker writes are fenced."""
    if followup:
        return finish_followup(lead_id, outcome, alert_text, delivery={
            "token": token, "timestamp": timestamp, "fields": fields, "chat_id": chat_id,
            "late_alert_text": late_alert_text, "success_status": success_status,
            "failed_status": failed_status,
        })
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return _record_send(conn, lead_id, token, outcome, timestamp, fields, chat_id,
                            alert_text, late_alert_text, success_status, failed_status)


def _record_send(conn: sqlite3.Connection, lead_id: int, token: str, outcome: str,
                 timestamp: str, fields: dict[str, Any], chat_id: str,
                 alert_text: Optional[str], late_alert_text: str,
                 success_status: str = "replied", failed_status: str = "needs_human"
                 ) -> tuple[Optional[str], bool]:
    row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    if row is None:
        return None, False
    valid = bool(token) and row["status"] == "sending" and row["claim_token"] == token and not row["worker_abandoned"]
    values = dict(fields)
    if outcome == "sent":
        conn.execute("UPDATE leads SET sent_at=?, replied_at=? WHERE id=?", (timestamp, timestamp, lead_id))
        if not valid:
            _queue_alert(conn, lead_id, chat_id, late_alert_text)
            return row["status"], True
        status = success_status
    else:
        if not valid:
            return row["status"], False
        status = "delivery_unknown" if outcome == "uncertain" else failed_status
    values["notify_chat_id"] = chat_id
    _transition(conn, lead_id, "sending", status, values, alert_text, token)
    return status, alert_text is not None


def finish_followup(lead_id: int, followup_status: str,
                    escalate_alert_text: Optional[str] = None, *,
                    delivery: Optional[dict[str, Any]] = None,
                    claim_token: Optional[str] = None) -> tuple[Optional[str], bool]:
    """Commit the outcome and any delivery escalation/outbox entry together."""
    if followup_status not in {"sent", "blocked", "failed", "uncertain"}:
        raise ValueError("invalid follow-up outcome")
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        if row is None:
            return None, False
        if delivery is not None:
            valid = (row["status"] == "sending" and row["claim_token"] == delivery["token"]
                     and not row["worker_abandoned"])
            if followup_status != "sent" and not valid:
                return row["status"], False
            if followup_status == "uncertain" and escalate_alert_text is None:
                escalate_alert_text = (f"Delivery to {row['email'] or '-'} may or may not have happened, "
                                       "check the sent folder before contacting the lead.")
            result = _record_send(conn, lead_id, outcome=followup_status,
                                  alert_text=escalate_alert_text, **delivery)
        elif claim_token is not None:
            target = "replied" if followup_status == "blocked" else "needs_human"
            if not _transition(conn, lead_id, "processing", target, {},
                               escalate_alert_text, claim_token):
                return row["status"], False
            result = target, escalate_alert_text is not None
        elif followup_status == "uncertain":
            # A completed send wins a race with a recovery snapshot.
            if row["followup_status"] not in {"claimed", "uncertain"}:
                return row["status"], False
            has_alert = row["followup_status"] == "uncertain" and row["notify_text"] is not None
            text = None if has_alert else (escalate_alert_text or
                f"Delivery to {row['email'] or '-'} may or may not have happened, "
                "check the sent folder before contacting the lead.")
            _transition(conn, lead_id, row["status"], "delivery_unknown", {}, text)
            result = "delivery_unknown", text is not None
        else:
            result = row["status"], False
        conn.execute("UPDATE leads SET followup_status=?, followup_sent_at=? WHERE id=?",
                     (followup_status, (delivery["timestamp"] if delivery else iso(utcnow()))
                      if followup_status == "sent" else None, lead_id))
        return result


def interrupted_followups(now: datetime) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE (followup_status='claimed' AND followup_claimed_at<?)"
            " OR (followup_status='uncertain' AND (status!='delivery_unknown' OR notify_text IS NULL))",
            (iso(now - timedelta(minutes=10)),),
        ).fetchall()
    return [dict(row) for row in rows]


def recovery_candidates(now: datetime) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE (status='received' AND"
            " (created_at<? OR processing_backlog=1 OR worker_abandoned=1))"
            " OR (status IN ('processing','sending') AND"
            " (COALESCE(status_updated_at,created_at)<? OR worker_abandoned=1)) ORDER BY created_at,id",
            (iso(now - timedelta(minutes=2)), iso(now - timedelta(minutes=10))),
        ).fetchall()
    return [dict(row) for row in rows]


def mark_stale_lead(lead_id: int, status: str, now: datetime, alert_text: str) -> bool:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        stale = conn.execute(
            "SELECT * FROM leads WHERE id=? AND status=?"
            " AND (COALESCE(status_updated_at,created_at)<? OR worker_abandoned=1)",
            (lead_id, status, iso(now - timedelta(minutes=10))),
        ).fetchone()
        fields = {
            "reason": "Processing interrupted; delivery may already have occurred",
            "notify_chat_id": get_settings().admin_telegram_chat_id,
        }
        if stale and stale["followup_sent"] and stale["followup_status"] is None:
            fields["followup_status"] = "blocked" if status != "sending" else None
        return bool(stale) and _transition(
            conn, lead_id, status, "delivery_unknown" if status == "sending" else "needs_human",
            fields, alert_text)


def record_backlog(lead_id: int) -> int:
    with connect() as conn:
        conn.execute("UPDATE leads SET processing_backlog=1 WHERE id=? AND status='received'", (lead_id,))
        return conn.execute("SELECT COUNT(*) FROM leads WHERE status='received'").fetchone()[0]


def has_backlog() -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM leads WHERE status='received' AND processing_backlog=1 LIMIT 1",
        ).fetchone() is not None


def received_leads(limit: int) -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM leads WHERE status='received' ORDER BY created_at,id LIMIT ?", (limit,),
        ).fetchall()
    return [row["id"] for row in rows]


def mark_worker_abandoned(lead_id: int, token: Optional[str], alert_text: str) -> bool:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        fence = "claim_token=?" if token is not None else "claim_token IS NULL AND status='received'"
        changed = conn.execute(f"UPDATE leads SET worker_abandoned=1 WHERE id=? AND {fence}",
                               (lead_id, token) if token is not None else (lead_id,)).rowcount
        if not changed:
            return False
        row = conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
        return _transition(conn, lead_id, row["status"],
                           "delivery_unknown" if row["status"] == "sending" else "needs_human", {
            "reason": "Worker abandoned; delivery may already have occurred",
            "notify_chat_id": get_settings().admin_telegram_chat_id,
        }, alert_text)


def _queue_alert(conn: sqlite3.Connection, lead_id: int, chat_id: str, text: str,
                 claim_token: Optional[str] = None) -> None:
    conn.execute(
        "UPDATE leads SET owner_notified=0, notify_attempts=0, notify_chat_id=?,"
        " notify_text=?, notify_claimed_at=NULL, notify_claim_token=?,"
        " notify_generation=notify_generation+1 WHERE id=?",
        (chat_id, text, claim_token, lead_id),
    )


def queue_alert(lead_id: int, chat_id: str, text: str) -> None:
    with connect() as conn:
        _queue_alert(conn, lead_id, chat_id, text)


def missing_alerts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE owner_notified=0 AND notify_text IS NULL AND"
            " (status IN ('pending_approval','needs_human','delivery_unknown')"
            " OR (status='replied' AND (score='hot' OR urgent=1 OR needs_human=1))"
            " OR (status='duplicate' AND repeat_note_needed=1))",
        ).fetchall()
    return [dict(row) for row in rows]


def repair_alert(lead_id: int, expected_status: str, chat_id: str, text: str) -> bool:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        missing = conn.execute(
            "SELECT id FROM leads WHERE id=? AND status=? AND owner_notified=0 AND notify_text IS NULL",
            (lead_id, expected_status),
        ).fetchone()
        if not missing:
            return False
        _queue_alert(conn, lead_id, chat_id, text)
        return True


def pending_alert_ids() -> list[int]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM leads WHERE owner_notified=0 AND notify_text IS NOT NULL"
            " AND notify_attempts<5",
        ).fetchall()
    return [row["id"] for row in rows]


def expire_exhausted_alerts(now: datetime) -> list[int]:
    """Escalate a crash during the last reserved attempt without sending again."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id FROM leads WHERE owner_notified=0 AND notify_attempts>=5"
            " AND notify_claimed_at<?", (iso(now - timedelta(minutes=10)),),
        ).fetchall()
        ids = [row["id"] for row in rows]
        for lead_id in ids:
            row = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
            _transition(conn, lead_id, row["status"], "needs_human", {}, None)
            conn.execute("UPDATE leads SET notify_claimed_at=NULL WHERE id=?", (lead_id,))
    return ids


def claim_alert(lead_id: int) -> Optional[tuple[int, int]]:
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE leads SET owner_notified=1, notify_claimed_at=NULL"
            f" WHERE id=? AND notify_claim_token IS NOT NULL AND NOT {DRAFT_FENCE}", (lead_id,),
        )
        now = utcnow()
        claimed = conn.execute(
            "UPDATE leads SET notify_attempts=notify_attempts+1, notify_claimed_at=?"
            " WHERE id=? AND owner_notified=0 AND notify_text IS NOT NULL AND notify_attempts<5"
            f" AND (notify_claimed_at IS NULL OR notify_claimed_at<?) AND {DRAFT_FENCE}",
            (iso(now), lead_id, iso(now - timedelta(minutes=10))),
        ).rowcount
        if not claimed:
            return None
        row = conn.execute("SELECT notify_generation, notify_attempts FROM leads WHERE id=?",
                           (lead_id,)).fetchone()
        return row["notify_generation"], row["notify_attempts"]


def alert_payload(lead_id: int, generation: int, attempt: int) -> Optional[tuple[str, str]]:
    with connect() as conn:
        row = conn.execute(
            "SELECT notify_chat_id, notify_text FROM leads WHERE id=?"
            f" AND notify_generation=? AND notify_attempts=? AND notify_claimed_at IS NOT NULL AND {DRAFT_FENCE}",
            (lead_id, generation, attempt),
        ).fetchone()
    return (row["notify_chat_id"], row["notify_text"]) if row else None


def finish_alert(lead_id: int, generation: int, attempt: int) -> bool:
    with connect() as conn:
        return conn.execute(
            "UPDATE leads SET owner_notified=1, notify_claimed_at=NULL"
            " WHERE id=? AND notify_generation=? AND notify_attempts=?"
            f" AND notify_claimed_at IS NOT NULL AND {DRAFT_FENCE}", (lead_id, generation, attempt),
        ).rowcount == 1


def fail_alert(lead_id: int, generation: int, attempt: int) -> bool:
    """Fail only the matching claim; return whether that claim exhausted its budget."""
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            "UPDATE leads SET notify_claimed_at=NULL WHERE id=? AND notify_generation=?"
            f" AND notify_attempts=? AND notify_claimed_at IS NOT NULL AND {DRAFT_FENCE}",
            (lead_id, generation, attempt),
        ).rowcount
        if not changed or attempt < 5:
            return False
        row = conn.execute("SELECT status FROM leads WHERE id=?", (lead_id,)).fetchone()
        _transition(conn, lead_id, row["status"], "needs_human", {}, None)
        return True


def get_due_followups(now: Optional[datetime] = None) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM leads WHERE status='replied' AND follow_up_at IS NOT NULL"
            " AND follow_up_at<=? AND followup_sent=0 AND booked=0 ORDER BY follow_up_at",
            (iso(now or utcnow()),),
        ).fetchall()
    return [dict(r) for r in rows]


def weekly_stats(days: int = 7, now: Optional[datetime] = None) -> dict[str, Any]:
    cutoff = iso((now or utcnow()) - timedelta(days=days))
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM leads WHERE created_at>=?", (cutoff,)).fetchall()]
    real = [r for r in rows if not r["is_duplicate"]]
    by_score: dict[str, int] = {}
    for r in real:
        key = r["score"] or "unscored"
        by_score[key] = by_score.get(key, 0) + 1
    deltas = [
        (datetime.fromisoformat(r["replied_at"]) - datetime.fromisoformat(r["created_at"])).total_seconds()
        for r in real if r["replied_at"]
    ]
    return {
        "days": days,
        "leads_received": len(real),
        "duplicates": len(rows) - len(real),
        "by_score": by_score,
        "needs_human": sum(1 for r in real if r["needs_human"]),
        "replied": len(deltas),
        "avg_response_seconds": round(sum(deltas) / len(deltas), 1) if deltas else None,
        "followups_sent": sum(1 for r in real if r["followup_status"] == "sent"),
        "followups_blocked": sum(1 for r in real if r["followup_status"] == "blocked"),
        "followups_failed": sum(1 for r in real if r["followup_status"] == "failed"),
        "followups_uncertain": sum(1 for r in real if r["followup_status"] == "uncertain"),
    }
