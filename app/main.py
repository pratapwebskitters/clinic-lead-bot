"""FastAPI entrypoint: webhook, health check, and owner approval endpoint."""
from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException

from app import clinics, db
from app.config import get_settings, missing_required
from app.models import LeadIn
from app.services import mailer, recovery, router, workers

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configs = clinics.validate_clinics()
    missing = missing_required(settings)
    if settings.app_env == "production" and any(config["mode"] == "auto" for config in configs):
        if not settings.smtp_host:
            missing.append("SMTP_HOST")
        if not settings.smtp_from:
            missing.append("SMTP_FROM")
    if missing:
        msg = f"Missing required environment variables: {', '.join(missing)}"
        if settings.app_env == "production":
            raise RuntimeError(msg)
        logger.warning(msg)
    db.init_db()
    workers.start(settings.lead_workers)
    recovery_worker = recovery.RecoveryWorker() if settings.enable_recovery else None
    scheduler = None
    scheduler_started = False
    try:
        if recovery_worker:
            recovery_worker.start()
        if settings.enable_scheduler:
            from apscheduler.schedulers.background import BackgroundScheduler

            from app.services import followup

            scheduler = BackgroundScheduler()
            scheduler.add_job(followup.run_followups, "interval",
                              minutes=settings.followup_interval_minutes)
            scheduler.start()
            scheduler_started = True
        yield
    finally:
        if scheduler_started:
            scheduler.shutdown(wait=False)
        if recovery_worker:
            recovery_worker.stop()
        workers.stop()


app = FastAPI(title="Clinic Lead Bot", lifespan=lifespan)


def _require_secret(provided: Optional[str], expected: str) -> None:
    if not expected or not provided or not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    ):
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook/lead")
def receive_lead(
    payload: LeadIn,
    background: BackgroundTasks,
    x_webhook_secret: Optional[str] = Header(default=None),
) -> dict:
    _require_secret(x_webhook_secret, get_settings().webhook_secret)

    # Save FIRST: the lead exists in the DB before any LLM call can fail.
    lead_id, duplicate_id = db.save_lead_dedup(
        clinic_id=payload.clinic_id, name=payload.name, phone=payload.phone,
        email=payload.email, message=payload.message, service=payload.service,
    )
    if duplicate_id is not None:
        background.add_task(workers.run_lead, lead_id, repeat=True)
        return {"lead_id": lead_id, "status": "duplicate", "duplicate_of": duplicate_id}

    background.add_task(workers.run_lead, lead_id)
    return {"lead_id": lead_id, "status": "received"}


@app.post("/admin/leads/{lead_id}/approve")
def approve(lead_id: int, x_admin_secret: Optional[str] = Header(default=None)) -> dict:
    _require_secret(x_admin_secret, get_settings().admin_secret)
    lead = db.get_lead(lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="lead not found")
    token = db.claim_processing(lead_id, "pending_approval")
    if token is None:
        raise HTTPException(status_code=409, detail=f"lead status is {lead['status']}")
    lead["claim_token"] = token
    try:
        outcome = router.approve_lead(lead)
    except router.UnsafeDraft as exc:
        raise HTTPException(status_code=422, detail=exc.reasons) from exc
    except router.LostClaim as exc:
        raise HTTPException(status_code=409, detail="lost claim") from exc
    except (clinics.ClinicConfigError, clinics.ClinicNotFound) as exc:
        router.transition_with_alert(lead, "processing", "needs_human",
                                     {"reason": str(exc)}, {}, "Approval blocked: clinic configuration invalid.")
        raise HTTPException(status_code=422, detail="Clinic configuration invalid") from exc
    if outcome is mailer.DeliveryResult.UNCERTAIN:
        raise HTTPException(status_code=502, detail=router.uncertain_explanation(lead))
    if outcome is mailer.DeliveryResult.FAILED:
        raise HTTPException(status_code=502, detail="reply could not be sent (no email or send failed)")
    return {"lead_id": lead_id, "status": db.lead_status(lead_id)}
