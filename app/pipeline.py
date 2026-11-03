"""The lead-processing pipeline. The lead is already saved before this runs."""
from __future__ import annotations

import logging
import time

from app import clinics, db
from app.config import get_settings
from app.services import llm, router, safety

logger = logging.getLogger(__name__)
DEADLINE = 45


def process_lead(lead_id: int) -> None:
    deadline = time.monotonic() + DEADLINE
    lead = None
    token = None
    try:
        token = db.claim_processing(lead_id)
        if token is None:
            return
        from app.services import workers

        workers.bind_claim(lead_id, token)
        lead = db.get_lead(lead_id)
        if lead is None or lead["is_duplicate"]:
            return
        _process(lead, deadline, token)
    except Exception as exc:
        logger.exception("pipeline failed for lead %s", lead_id)
        details = (
            f"{lead['name']} {lead['phone']}\nMessage: {lead['message'][:300]}"
            if lead else "Lead details could not be loaded."
        )
        try:
            if token is not None:
                for status in ("processing", "sending"):
                    changed = db.transition(lead_id, status,
                                            "delivery_unknown" if status == "sending" else "needs_human", {
                        "reason": f"pipeline error: {exc}"[:300],
                        "notify_chat_id": get_settings().admin_telegram_chat_id,
                    }, f"⚠️ Lead #{lead_id} failed processing and needs a human.\n{details}", claim_token=token)
                    if changed:
                        router.send_pending_alert(lead_id, deadline=deadline)
                        break
        except Exception:
            logger.exception("could not record failure for lead %s", lead_id)


def _process(lead: dict, deadline: float, token: str) -> None:
    clinic = clinics.load_clinic(lead["clinic_id"])
    result = llm.qualify(lead, clinic, deadline=deadline)
    if time.monotonic() >= deadline:
        result = llm.fallback_result()
        result.reason = "Processing deadline exceeded"

    if result.score == "spam" and not result.urgent and not result.llm_failed:
        if not db.transition(lead["id"], "processing", "spam", {
            "score": "spam", "intent": result.intent, "language": result.language, "reason": result.reason,
        }, claim_token=token):
            logger.info("lost claim for lead %s", lead["id"])
        return

    score = "hot" if result.urgent and result.score == "spam" else result.score
    reply = (safety.safe_reply(clinic) if result.urgent or result.llm_failed
             else (result.reply or "").strip())
    needs_human = result.needs_human or result.urgent or result.llm_failed
    ok, reasons = safety.check_reply(reply, clinic)
    if not ok:
        logger.warning("lead %s reply rejected: %s", lead["id"], reasons)
        reply = safety.safe_reply(clinic)
        needs_human = True
        fallback_ok, fallback_reasons = safety.check_reply(reply, clinic)
        if not fallback_ok:
            if not db.update_claimed(lead["id"], token, {
                "reply": None, "safety_flags": "; ".join(reasons + fallback_reasons),
            }):
                logger.info("lost claim for lead %s", lead["id"])
                return
            raise ValueError("fallback reply rejected by safety checks")

    if not db.update_claimed(lead["id"], token, {
        "score": score, "intent": result.intent, "language": result.language,
        "urgent": int(result.urgent), "needs_human": int(needs_human), "reason": result.reason,
        "reply": reply, "safety_flags": "; ".join(reasons) or None,
    }):
        logger.info("lost claim for lead %s", lead["id"])
        return
    fresh = db.get_lead(lead["id"]) or lead
    fresh["claim_token"] = token  # Never adopt a different worker's token from a refreshed row.
    router.route(fresh, clinic, score=score, urgent=result.urgent,
                 needs_human=needs_human, reply=reply, llm_failed=result.llm_failed, deadline=deadline)
