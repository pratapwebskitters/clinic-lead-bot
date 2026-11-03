"""SMTP delivery with explicit confirmed, rejected, and uncertain outcomes."""
from __future__ import annotations

import logging
import smtplib
import ssl
import time
from email.message import EmailMessage
from enum import Enum
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)


class DeliveryResult(Enum):
    SENT = "sent"
    FAILED = "failed"
    UNCERTAIN = "uncertain"


def _timeout(deadline: float | None) -> float:
    remaining = deadline - time.monotonic() if deadline is not None else 15
    if remaining <= 0:
        raise TimeoutError("Lead processing deadline exceeded before transmission")
    return min(15, remaining)


def _set_timeout(server: smtplib.SMTP, deadline: float | None) -> None:
    timeout = _timeout(deadline)
    if server.sock is not None:
        server.sock.settimeout(timeout)


def send_email(to: str, subject: str, body: str, from_name: Optional[str] = None,
               deadline: float | None = None) -> DeliveryResult:
    server = None
    try:
        deadline = deadline if deadline is not None else time.monotonic() + 15
        s = get_settings()
        if not (s.smtp_host and s.smtp_from):
            logger.warning("SMTP not configured; email to %s skipped", to)
            return DeliveryResult.FAILED
        msg = EmailMessage()
        msg["To"] = to
        msg["Subject"] = subject
        msg["From"] = f"{from_name} <{s.smtp_from}>" if from_name else s.smtp_from
        msg.set_content(body)
        server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=_timeout(deadline))
        if s.smtp_use_tls:
            _set_timeout(server, deadline)
            server.starttls(context=ssl.create_default_context())
        if s.smtp_user:
            _set_timeout(server, deadline)
            server.login(s.smtp_user, s.smtp_password)
        _set_timeout(server, deadline)
        try:
            server.send_message(msg)
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError) as exc:
            logger.error("Email rejected: %s", type(exc).__name__)
            return DeliveryResult.FAILED
        except Exception as exc:
            logger.error("Email delivery uncertain: %s", type(exc).__name__)
            return DeliveryResult.UNCERTAIN
        return DeliveryResult.SENT
    except Exception as exc:
        logger.error("Email setup failed: %s", type(exc).__name__)
        return DeliveryResult.FAILED
    finally:
        if server is not None:
            try:
                if deadline is not None and server.sock is not None:
                    server.sock.settimeout(max(0.001, min(1, deadline - time.monotonic())))
                server.quit()
            except Exception as exc:
                logger.warning("SMTP shutdown failed: %s", type(exc).__name__)
            finally:
                try:
                    server.close()
                except Exception as exc:
                    logger.warning("SMTP close failed: %s", type(exc).__name__)
