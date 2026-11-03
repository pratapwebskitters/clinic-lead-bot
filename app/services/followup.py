"""Send at most one follow-up to warm leads that were replied to but have not booked."""
from __future__ import annotations

import logging
import unicodedata
from datetime import datetime
from typing import Optional

from app import clinics, db
from app.services import mailer, router, safety

logger = logging.getLogger(__name__)


def followup_text(lead: dict, clinic: dict) -> str:
    name = "".join(c for c in lead["name"] if not unicodedata.category(c).startswith("C"))[:50]
    return (
        f"Hi {name}, just checking in from {clinic.get('clinic_name', 'our clinic')}. "
        f"If you'd still like a consultation, you can pick a time here: {clinic.get('booking_link', '')}. "
        f"Call us on {clinic.get('phone', '')} if you have any questions."
    )


def run_followups(now: Optional[datetime] = None) -> int:
    sent = 0
    for lead in db.get_due_followups(now):
        token = None
        sending = False
        finishing = False
        outcome = mailer.DeliveryResult.FAILED
        clinic = {}
        try:
            token = db.claim_followup(lead["id"])
            if token is None:
                continue
            lead["claim_token"] = token
            clinic = clinics.load_clinic(lead["clinic_id"])
            reply = followup_text(lead, clinic)
            ok, reasons = safety.check_reply(reply, clinic)
            if not ok:
                finishing = True
                status, _ = db.finish_followup(lead["id"], "blocked", claim_token=token)
                if status != "replied":
                    logger.info("lost claim for follow-up lead %s", lead["id"])
                logger.warning("follow-up for lead %s rejected: %s", lead["id"], reasons)
                continue
            if not router.begin_delivery(lead):
                continue
            sending = True
            if lead.get("email"):
                outcome = db.external_call("email", mailer.send_email,
                    to=lead["email"],
                    subject=f"Following up: {clinic.get('clinic_name', 'your enquiry')}",
                    body=reply,
                    from_name=clinic.get("clinic_name"),
                )
            finishing = True
            router.record_delivery(lead, clinic, outcome, followup=True)
            if outcome is mailer.DeliveryResult.SENT:
                sent += 1
            elif outcome is mailer.DeliveryResult.UNCERTAIN:
                logger.warning("follow-up for lead %s delivery uncertain; no retry", lead["id"])
            else:
                logger.warning("follow-up for lead %s could not be sent; no retry", lead["id"])
        except Exception:
            logger.exception("follow-up failed for lead %s", lead["id"])
            if token is not None and not finishing:
                if sending:
                    router.record_delivery(lead, clinic, outcome, followup=True)
                else:
                    _, queued = db.finish_followup(lead["id"], "failed",
                        "Follow-up could not be prepared; contact the lead manually.", claim_token=token)
                    if queued:
                        router.send_pending_alert(lead["id"])
    return sent
