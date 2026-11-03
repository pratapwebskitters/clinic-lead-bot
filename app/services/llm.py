"""LLM qualification. Provider call is isolated in call_provider() so tests can mock it."""
from __future__ import annotations

import json
import logging
import time
from collections.abc import Iterator
from html import escape

import httpx
import yaml

from app import db
from app.config import get_settings
from app.models import QualificationResult

logger = logging.getLogger(__name__)

PRIVATE_KEYS = {"owner_telegram_chat_id", "owner_email", "mode"}
MAX_RESPONSE_BYTES = 64 * 1024


class DeadlineExceeded(TimeoutError):
    pass


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise DeadlineExceeded("Lead processing deadline exceeded")
    return remaining


class _DeadlineStream(httpx.SyncByteStream):
    """Check raw chunks too: decompression may buffer trickles without yielding bytes."""
    def __init__(self, stream: httpx.SyncByteStream, deadline: float) -> None:
        self.stream = stream
        self.deadline = deadline

    def __iter__(self) -> Iterator[bytes]:
        size = 0
        for chunk in self.stream:
            _remaining(self.deadline)
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise ValueError("LLM response exceeds 64 KB")
            yield chunk

    def close(self) -> None:
        self.stream.close()


def _read_provider(url: str, headers: dict, payload: dict, timeout: float, deadline: float) -> dict:
    limits = httpx.Timeout(min(timeout, _remaining(deadline)))
    with httpx.stream("POST", url, headers=headers, json=payload, timeout=limits) as response:
        _remaining(deadline)
        response.raise_for_status()
        response.stream = _DeadlineStream(response.stream, deadline)
        body = bytearray()
        # No chunk_size: buffering trickles into fixed-size chunks would hide elapsed time.
        for chunk in response.iter_bytes():
            _remaining(deadline)
            if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
                raise ValueError("LLM response exceeds 64 KB")
            body.extend(chunk)
        _remaining(deadline)
    return json.loads(body)


def build_system_prompt(clinic: dict) -> str:
    template = (get_settings().prompts_dir / "qualify.md").read_text(encoding="utf-8")
    public = {k: v for k, v in clinic.items() if k not in PRIVATE_KEYS}
    info = yaml.safe_dump(public, allow_unicode=True, sort_keys=False).strip()
    return (
        template.replace("{{clinic_name}}", str(clinic.get("clinic_name", "the clinic")))
        .replace("{{business_info}}", info)
    )


def build_user_message(lead: dict) -> str:
    return (
        "<lead>\n"
        f"Name: {escape(str(lead.get('name', '')))}\n"
        f"Service interested in: {escape(str(lead.get('service') or 'not specified'))}\n"
        f"Message: {escape(str(lead.get('message', '')))}\n"
        "</lead>"
    )


def call_provider(system: str, user: str, timeout: float | None = None,
                  deadline: float | None = None) -> str:
    """Single place that talks to the LLM API. Returns the raw text of the model reply."""
    s = get_settings()
    timeout = min(s.llm_timeout, timeout) if timeout is not None else s.llm_timeout
    deadline = deadline if deadline is not None else time.monotonic() + timeout
    _remaining(deadline)
    if s.llm_provider == "anthropic":
        data = _read_provider(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": s.llm_api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload={
                "model": s.llm_model,
                "max_tokens": 600,
                "temperature": 0.2,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
            timeout=timeout, deadline=deadline,
        )
        blocks = data.get("content", [])
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    if s.llm_provider == "openai":
        data = _read_provider(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {s.llm_api_key}"},
            payload={
                "model": s.llm_model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            timeout=timeout, deadline=deadline,
        )
        return data["choices"][0]["message"]["content"]

    raise RuntimeError(f"unsupported LLM_PROVIDER: {s.llm_provider!r}")


def extract_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in LLM output")
    return json.loads(text[start : end + 1])


def fallback_result() -> QualificationResult:
    return QualificationResult(
        language="unknown", intent="other", score="warm", urgent=False,
        needs_human=True, reason="LLM unavailable or returned invalid output", reply="",
        llm_failed=True,
    )


def qualify(lead: dict, clinic: dict, deadline: float | None = None) -> QualificationResult:
    """Try twice, then fall back to a result that forces human handling."""
    system, user = build_system_prompt(clinic), build_user_message(lead)
    for attempt in (1, 2):
        started = time.monotonic()
        remaining = deadline - started if deadline is not None else get_settings().llm_timeout
        if remaining <= 0 or (deadline is not None and attempt == 2 and remaining < 10):
            break
        try:
            result = QualificationResult.model_validate(extract_json(db.external_call("llm", call_provider,
                system, user, timeout=min(get_settings().llm_timeout, remaining), deadline=deadline)))
            if deadline is not None and time.monotonic() >= deadline:
                return fallback_result()
            return result
        except DeadlineExceeded:
            logger.warning("LLM processing deadline exceeded; not retrying")
            break
        except Exception as exc:  # network, JSON, or validation errors all retry once
            logger.warning("LLM attempt %s failed: %s", attempt, exc)
            if attempt == 1 and time.monotonic() - started > 25:
                break
    return fallback_result()
