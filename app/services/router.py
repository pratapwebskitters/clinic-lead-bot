"""Routing: decide what happens after qualification (reply, alert, follow-up, shadow mode)."""
from __future__ import annotations

from datetime import timedelta
import logging
import time

from app import clinics, db
from app.config import get_settings
from app.services import mailer, notify, safety

logger = logging.getLogger(__name__)


def send_pending_alert(lead_id: int, deadline: float | None = None) -> None:
    claim = db.claim_alert(lead_id)
    if claim is None:
        return
    generation, attempt = claim
    payload = db.alert_payload(lead_id, generation, attempt)
    if payload is None:
        return
    try:
        delivered = db.external_call("telegram", notify.send_telegram, *payload, **({"deadline": deadline} if deadline is not None else {}))
    except Exception:
        delivered = False
    if delivered:
        db.finish_alert(lead_id, generation, attempt)
    elif db.fail_alert(lead_id, generation, attempt):
        logger.error("Alert retries exhausted for lead %s; needs human attention", lead_id)


def send_alert(lead_id: int, chat_id: str, text: str) -> None:
    db.queue_alert(lead_id, chat_id, text)
    send_pending_alert(lead_id)


def owner_chat(clinic: dict) -> str:
    chat = str(clinic.get("owner_telegram_chat_id") or "")
    if not chat or chat.startswith("REPLACE"):
        chat = get_settings().admin_telegram_chat_id
    return chat


def deliver_reply(lead: dict, clinic: dict, reply: str,
                  deadline: float | None = None) -> mailer.DeliveryResult:
    """Send the reply to the lead. Currently email only; WhatsApp plugs in here later."""
    email = lead.get("email")
    if not email or not reply:
        return mailer.DeliveryResult.FAILED
    return db.external_call("email", mailer.send_email,
        to=email,
        subject=f"Re: your enquiry at {clinic.get('clinic_name', 'our clinic')}",
        body=reply,
        from_name=clinic.get("clinic_name"),
        **({"deadline": deadline} if deadline is not None else {}),
    )


def _owner_message(lead: dict, *, score: str, urgent: bool, needs_human: bool, reply: str, note: str) -> str:
    flags = []
    if urgent:
        flags.append("URGENT")
    if needs_human:
        flags.append("NEEDS HUMAN")
    icon = "🚨" if urgent else ("🔥" if score == "hot" else "📩")
    head = f"{icon} Lead #{lead['id']} [{score.upper()}] {' '.join(flags)}".strip()
    return (
        f"{head}\n"
        f"Name: {lead['name']}\nPhone: {lead['phone']}\nEmail: {lead.get('email') or '-'}\n"
        f"Message: {lead['message'][:400]}\n\n"
        f"Reply: {reply}\n\n{note}"
    )


def _schedule_followup(lead_id: int, score: str, needs_human: bool) -> dict:
    if score == "warm" and not needs_human:
        hours = get_settings().followup_delay_hours
        return {"follow_up_at": db.iso(db.utcnow() + timedelta(hours=hours))}
    return {}


def uncertain_explanation(lead: dict) -> str:
    return (f"Delivery to {lead.get('email') or '-'} may or may not have happened, "
            "check the sent folder before contacting the lead.")


def transition_with_alert(lead: dict, expected_status: str, new_status: str,
                          fields: dict, clinic: dict, note: str, deadline: float | None = None) -> bool:
    token = lead.get("claim_token")
    if not token:
        logger.info("lost claim for lead %s", lead["id"])
        return False
    fresh = dict(lead, **fields)
    text = _owner_message(
        fresh, score=fresh.get("score") or "unknown", urgent=bool(fresh.get("urgent")),
        needs_human=new_status in {"needs_human", "delivery_unknown"} or bool(fresh.get("needs_human")),
        reply=fresh.get("reply") or "", note=note)
    changed = db.transition(lead["id"], expected_status, new_status,
                            dict(fields, notify_chat_id=owner_chat(clinic)), text, claim_token=token)
    if changed:
        send_pending_alert(lead["id"], deadline=deadline)
    else:
        logger.info("lost claim for lead %s", lead["id"])
    return changed


def begin_delivery(lead: dict) -> bool:
    if not db.begin_send(lead["id"], lead.get("claim_token") or ""):
        logger.info("lost claim for lead %s", lead["id"])
        return False
    return True


def record_delivery(lead: dict, clinic: dict, outcome: mailer.DeliveryResult,
                    fields: dict | None = None, needs_human: bool = False,
                    success_status: str = "replied", failed_status: str = "needs_human",
                    followup: bool = False, deadline: float | None = None) -> str:
    timestamp = db.iso(db.utcnow())
    if outcome is mailer.DeliveryResult.SENT:
        note = "Reply sent to the lead."
    elif outcome is mailer.DeliveryResult.UNCERTAIN:
        note = uncertain_explanation(lead)
    else:
        note = "Could NOT send the reply. Contact the lead manually."
    requires_alert = (outcome is not mailer.DeliveryResult.SENT or needs_human
                      or lead.get("score") == "hot" or lead.get("urgent"))
    text = _owner_message(lead, score=lead.get("score") or "unknown", urgent=bool(lead.get("urgent")),
                          needs_human=needs_human or outcome is not mailer.DeliveryResult.SENT,
                          reply=lead.get("reply") or "", note=note) if requires_alert else None
    late_text = (f"An automatic reply WAS sent to {lead.get('email') or '-'} at {timestamp} "
                 "while this lead was escalated, check before contacting them.")
    status, queued = db.record_send(
        lead["id"], lead.get("claim_token") or "", outcome.value, timestamp, fields or {},
        owner_chat(clinic), text, late_text, success_status=success_status,
        followup=followup, failed_status=failed_status)
    if queued:
        try:
            send_pending_alert(lead["id"], deadline=deadline)
        except Exception:
            logger.exception("Delivery recorded; owner alert remains queued for lead %s", lead["id"])
    return status or "lost_claim"


def rebuild_alert(lead: dict) -> tuple[str, str]:
    try:
        clinic = clinics.load_clinic(lead["clinic_id"])
    except Exception:
        clinic = {}
    status = lead["status"]
    if status == "duplicate":
        return owner_chat(clinic), (
            f"Low priority: Repeat enquiry from {lead['name']} with a new message: {lead['message']}"
        )
    if status == "delivery_unknown":
        note = uncertain_explanation(lead)
    elif status == "pending_approval":
        note = f"SHADOW MODE: not sent to the lead. Approve: POST /admin/leads/{lead['id']}/approve"
    else:
        note = f"Recovered missing owner alert. Status: {status}. {lead.get('reason') or ''}"
    return owner_chat(clinic), _owner_message(
        lead, score=lead.get("score") or "unknown", urgent=bool(lead.get("urgent")),
        needs_human=bool(lead.get("needs_human")) or status in {"needs_human", "delivery_unknown"},
        reply=lead.get("reply") or "", note=note)


def notify_repeat(lead_id: int) -> None:
    lead = db.get_lead(lead_id)
    if lead is None or not lead["repeat_note_needed"]:
        return
    chat_id, text = rebuild_alert(lead)
    if db.repair_alert(lead_id, "duplicate", chat_id, text):
        send_pending_alert(lead_id)


def route(lead: dict, clinic: dict, *, score: str, urgent: bool, needs_human: bool,
          reply: str, llm_failed: bool = False, deadline: float | None = None) -> str:
    """Apply routing rules and return the final lead status."""
    lead_id = lead["id"]

    if clinic.get("mode", "shadow") != "auto":
        if not begin_delivery(lead):
            return "lost_claim"
        status = "needs_human" if llm_failed else "pending_approval"
        note = ("SHADOW MODE: LLM failed; contact the lead manually." if llm_failed else
                f"SHADOW MODE: not sent to the lead. Approve: POST /admin/leads/{lead_id}/approve")
        fields = {"notify_claim_token": lead["claim_token"]} if status == "pending_approval" else {}
        transition_with_alert(lead, "sending", status, fields, clinic, note, deadline=deadline)
        return status

    expired = deadline is not None and time.monotonic() >= deadline
    if expired:
        reply = _deadline_fallback(lead, clinic)
        if reply is None:
            return "lost_claim"
        llm_failed, needs_human = True, True
    if not begin_delivery(lead):
        return "lost_claim"
    # After expiry, allow one bounded template attempt. Never resend an uncertain message.
    delivered = deliver_reply(lead, clinic, reply, deadline=None if expired else deadline)
    if (delivered is mailer.DeliveryResult.FAILED and not expired
            and deadline is not None and time.monotonic() >= deadline):
        if not db.transition(lead_id, "sending", "processing", {}, claim_token=lead["claim_token"]):
            logger.info("lost claim for lead %s", lead_id)
            return "lost_claim"
        reply = _deadline_fallback(lead, clinic)
        if reply is None or not begin_delivery(lead):
            return "lost_claim"
        llm_failed, needs_human = True, True
        delivered = deliver_reply(lead, clinic, reply)
    if deadline is not None and time.monotonic() >= deadline:
        llm_failed, needs_human = True, True
    fields = {}
    if delivered is mailer.DeliveryResult.SENT:
        status = "needs_human" if llm_failed else "replied"
        fields.update(_schedule_followup(lead_id, score, needs_human))
    else:
        status = "replied"
        needs_human = True
    return record_delivery(lead, clinic, delivered, fields, needs_human=needs_human,
                           success_status=status, deadline=deadline)


def _deadline_fallback(lead: dict, clinic: dict) -> str | None:
    reply = safety.safe_reply(clinic)
    ok, reasons = safety.check_reply(reply, clinic)
    if not ok:
        raise UnsafeDraft(reasons)
    if not db.update_claimed(lead["id"], lead.get("claim_token") or "", {
        "reply": reply, "needs_human": 1, "reason": "Processing deadline exceeded",
    }):
        logger.info("lost claim for lead %s", lead["id"])
        return None
    lead.update(reply=reply, needs_human=1)
    return reply


class UnsafeDraft(ValueError):
    def __init__(self, reasons: list[str]):
        self.reasons = reasons
        super().__init__("; ".join(reasons))


class LostClaim(RuntimeError):
    pass


def approve_lead(lead: dict) -> mailer.DeliveryResult:
    """Reload clinic rules, recheck the stored draft, then send the claimed approval."""
    clinic = clinics.load_clinic(lead["clinic_id"])
    ok, reasons = safety.check_reply(lead.get("reply") or "", clinic)
    if not ok:
        transition_with_alert(lead, "processing", "needs_human", {
            "safety_flags": "; ".join(reasons),
        }, clinic, "Approval blocked by safety checks: " + "; ".join(reasons))
        raise UnsafeDraft(reasons)
    if not begin_delivery(lead):
        raise LostClaim("lost claim")
    try:
        delivered = deliver_reply(lead, clinic, lead.get("reply") or "")
    except Exception:
        delivered = mailer.DeliveryResult.FAILED
    fields = (_schedule_followup(lead["id"], lead.get("score") or "", bool(lead.get("needs_human")))
              if delivered is mailer.DeliveryResult.SENT else {})
    record_delivery(lead, clinic, delivered, fields, needs_human=bool(lead.get("needs_human")),
                    failed_status="pending_approval")
    return delivered
