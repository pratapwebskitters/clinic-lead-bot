"""Telegram notifications. Never raises: a failed alert must not break the pipeline."""
from __future__ import annotations

import logging
import time
from collections.abc import Iterator

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)


class _DeadlineStream(httpx.SyncByteStream):
    def __init__(self, stream: httpx.SyncByteStream, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self.stream:
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Telegram deadline exceeded")
            yield chunk

    def close(self) -> None:
        self.stream.close()


def send_telegram(chat_id: str, text: str, deadline: float | None = None) -> bool:
    token = get_settings().telegram_bot_token
    if not token or not chat_id:
        logger.warning("Telegram not configured; alert skipped: %s", text[:80])
        return False
    try:
        deadline = deadline if deadline is not None else time.monotonic() + 10
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Telegram deadline exceeded")
        with httpx.stream(
            "POST",
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:4000]},
            timeout=httpx.Timeout(min(10, remaining)),
        ) as resp:
            resp.raise_for_status()
            resp.stream = _DeadlineStream(resp.stream, deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError("Telegram deadline exceeded")
            for _ in resp.iter_bytes():
                if time.monotonic() >= deadline:
                    raise TimeoutError("Telegram deadline exceeded")
            if time.monotonic() >= deadline:
                raise TimeoutError("Telegram deadline exceeded")
        return True
    except Exception as exc:
        if isinstance(exc, httpx.HTTPStatusError):
            logger.error("Telegram send failed: %s (HTTP %s)",
                         type(exc).__name__, exc.response.status_code)
        else:
            logger.error("Telegram send failed: %s", type(exc).__name__)
        return False
