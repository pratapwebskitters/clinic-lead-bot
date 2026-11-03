"""Recover abandoned work without retrying potentially delivered replies."""
from __future__ import annotations

import logging
import time
from datetime import datetime
from threading import Event, Thread

from app import db
from app.services import router, workers

logger = logging.getLogger(__name__)


class RecoveryWorker:
    def __init__(self) -> None:
        self.stopping = Event()
        self.thread = Thread(target=self._run, name="recovery", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.stopping.is_set():
            started = time.monotonic()
            try:
                run_recovery()
            except Exception:
                logger.exception("Recovery tick failed")
            if self.stopping.wait(max(0, 60 - (time.monotonic() - started))):
                break

    def stop(self) -> None:
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join()


def run_recovery(now: datetime | None = None) -> None:
    current = now or db.utcnow()
    workers.watchdog()
    for lead_id in db.expire_exhausted_alerts(current):
        logger.error("Alert retries exhausted for lead %s after interrupted attempt", lead_id)
    for lead in db.interrupted_followups(current):
        try:
            db.finish_followup(lead["id"], "uncertain", router.uncertain_explanation(lead))
        except Exception:
            logger.exception("Could not recover follow-up for lead %s", lead["id"])
    for lead in db.missing_alerts():
        try:
            chat_id, text = router.rebuild_alert(lead)
            db.repair_alert(lead["id"], lead["status"], chat_id, text)
        except Exception:
            logger.exception("Could not repair missing alert for lead %s", lead["id"])
    # Snapshot existing retries so new failures wait until the next recovery pass.
    pending = db.pending_alert_ids()
    for lead in db.recovery_candidates(current):
        try:
            if lead["status"] == "received" and not lead["worker_abandoned"]:
                workers.submit_lead(lead["id"])
            else:
                text = (f"⚠️ Lead #{lead['id']} was interrupted in {lead['status']} and needs a human.\n"
                        "A reply may already have been sent. Do not automatically retry.\n"
                        f"{lead['name']} {lead['phone']}")
                if db.mark_stale_lead(lead["id"], lead["status"], current, text):
                    router.send_pending_alert(lead["id"])
        except Exception:
            logger.exception("Recovery failed for lead %s", lead["id"])
    for lead_id in pending:
        try:
            router.send_pending_alert(lead_id)
        except Exception:
            logger.exception("Alert recovery failed for lead %s", lead_id)


def refill_backlog() -> bool:
    """A freed slot wakes this sweep; select saved received leads oldest first."""
    if not db.has_backlog():
        return False
    for lead_id in db.received_leads(workers.capacity() + 1):
        if workers.submit_lead(lead_id, report_backlog=False) is None:
            return True
    return db.has_backlog()
