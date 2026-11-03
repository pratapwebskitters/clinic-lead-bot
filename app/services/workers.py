"""Bounded lead processing shared by webhooks and recovery."""
from __future__ import annotations

import logging
import time
from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event, RLock, Thread, get_ident
from typing import Any

from app import db, pipeline
from app.config import get_settings
from app.services import notify, router

logger = logging.getLogger(__name__)
_lock = RLock()
_executor: ThreadPoolExecutor | None = None
_capacity = 0
_pending: dict[tuple[int, bool], Future] = {}
_active: dict[tuple[int, bool], dict[str, Any]] = {}
_wake = Event()
_stopped = Event()
_monitor_thread: Thread | None = None
_stopping = False
_last_backlog_alert: float | None = None
_backlog_hint = False
_backlog_version = 0


def start(count: int | None = None) -> None:
    global _executor, _capacity, _monitor_thread, _last_backlog_alert, _backlog_hint, _backlog_version
    with _lock:
        if _executor is None and not _stopping:
            count = count or get_settings().lead_workers
            _executor = ThreadPoolExecutor(max_workers=count, thread_name_prefix="lead")
            _capacity = count * 2
            _last_backlog_alert = None
            _backlog_hint, _backlog_version = False, 0
            _wake.clear()
            _stopped.clear()
            _monitor_thread = Thread(target=_monitor, name="lead-watchdog", daemon=True)
            _monitor_thread.start()


def _monitor() -> None:
    global _backlog_hint
    from app.services import recovery

    while not _stopped.is_set():
        refill = _wake.wait(timeout=1)
        _wake.clear()
        if _stopped.is_set():
            break
        try:
            watchdog()
            with _lock:
                needed, version = _backlog_hint, _backlog_version
            if refill and needed:
                remaining = recovery.refill_backlog()
                with _lock:
                    if not remaining and version == _backlog_version:
                        _backlog_hint = False
        except Exception:
            logger.exception("Lead pool monitor failed")


def _execute(key: tuple[int, bool]) -> None:
    with _lock:
        _active[key] = {"lead_id": key[0], "repeat": key[1], "thread_id": get_ident(),
                        "started_at": time.monotonic(), "abandoned": False}
    try:
        function = router.notify_repeat if key[1] else pipeline.process_lead
        function(key[0])
    finally:
        with _lock:
            _active.pop(key, None)


def bind_claim(lead_id: int, token: str) -> None:
    with _lock:
        for worker in _active.values():
            if worker["lead_id"] == lead_id and worker["thread_id"] == get_ident():
                worker["claim_token"] = token


def _finished(key: tuple[int, bool], future: Future) -> None:
    with _lock:
        _pending.pop(key, None)
    if not future.cancelled() and future.exception() is not None:
        logger.error("Lead worker failed for lead %s: %s", key[0], type(future.exception()).__name__)
    _wake.set()


def submit_lead(lead_id: int, repeat: bool = False, report_backlog: bool = True) -> Future | None:
    with _lock:
        if _stopping:
            return None
        start()
        key = (lead_id, repeat)
        if key in _pending:
            return _pending[key]
        if len(_pending) < _capacity:
            assert _executor is not None
            future = _executor.submit(_execute, key)
            _pending[key] = future
            future.add_done_callback(lambda completed: _finished(key, completed))
            return future
    if report_backlog:
        _report_backlog(lead_id)
    return None


def _report_backlog(lead_id: int) -> None:
    global _last_backlog_alert, _backlog_hint, _backlog_version
    waiting = db.record_backlog(lead_id)
    now = time.monotonic()
    with _lock:
        _backlog_hint = True
        _backlog_version += 1
        alert = _last_backlog_alert is None or now - _last_backlog_alert >= 300
        if alert:
            _last_backlog_alert = now
    _wake.set()
    if alert:
        text = f"processing backlog: {waiting} leads waiting"
        logger.warning("%s", text)
        db.external_call("telegram", notify.send_telegram, get_settings().admin_telegram_chat_id, text)


def stats() -> dict[str, Any]:
    with _lock:
        active = [dict(worker) for worker in _active.values()]
        return {"active": len(active), "queued": sum(not future.running() and not future.done()
                                                     for future in _pending.values()),
                "oldest_active_start_time": min((worker["started_at"] for worker in active), default=None),
                "abandoned": sum(worker["abandoned"] for worker in active),
                "workers": active}


def capacity() -> int:
    with _lock:
        return _capacity


def watchdog() -> None:
    now = time.monotonic()
    with _lock:
        overdue = [worker for worker in _active.values() if not worker["abandoned"]
                   and now - worker["started_at"] > pipeline.DEADLINE + 30]
        for worker in overdue:
            worker["abandoned"] = True
        overdue = [dict(worker) for worker in overdue]
    for worker in overdue:
        lead_id = worker["lead_id"]
        text = (f"Worker {worker['thread_id']} for lead #{lead_id} exceeded DEADLINE + 30 seconds; abandoned. "
                "A reply may already have been sent. Do not automatically retry.")
        logger.error("%s", text)
        try:
            if db.mark_worker_abandoned(lead_id, worker.get("claim_token"), text):
                router.send_pending_alert(lead_id)
            else:
                db.external_call("telegram", notify.send_telegram, get_settings().admin_telegram_chat_id, text)
        except Exception:
            logger.exception("Could not persist abandonment for lead %s", lead_id)
            db.external_call("telegram", notify.send_telegram, get_settings().admin_telegram_chat_id, text)


def run_lead(lead_id: int, repeat: bool = False) -> None:
    future = submit_lead(lead_id, repeat)
    if future is not None:
        future.result()


def wait_for_idle() -> None:
    while True:
        with _lock:
            pending = list(_pending.values())
        if not pending:
            return
        for future in pending:
            future.result()


def stop() -> None:
    global _executor, _monitor_thread, _stopping
    with _lock:
        _stopping = True
        executor, _executor = _executor, None
        monitor, _monitor_thread = _monitor_thread, None
    _stopped.set()
    _wake.set()
    try:
        if monitor is not None:
            monitor.join()
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)
    finally:
        with _lock:
            _stopping = False
