"""Code-level guardrails for LLM replies. These cannot be talked around by a prompt."""
from __future__ import annotations

import re
import logging
from decimal import Decimal

MAX_WORDS = 80
logger = logging.getLogger(__name__)

# Numeric prices written in words ("three hundred rupees") cannot be checked here.
# Non-price factual inventions also escape these checks; keep shadow mode on
# until the clinic has reviewed real drafts.

FORBIDDEN = [
    ("guarantee", re.compile(r"\bguarantee[sd]?\b", re.I)),
    ("cure", re.compile(r"\bcure[sd]?\b", re.I)),
    ("100%", re.compile(r"100\s?%")),
    ("painless", re.compile(r"\bpainless\b", re.I)),
]

MEDICATION_TERMS = [
    "ibuprofen", "paracetamol", "acetaminophen", "aspirin", "diclofenac", "naproxen",
    "amoxicillin", "azithromycin", "metronidazole", "antibiotic", "antibiotics",
    "painkiller", "painkillers", "tablet", "tablets", "dosage",
]
MEDICATION_RE = re.compile(r"\b(" + "|".join(MEDICATION_TERMS) + r")\b", re.I)

NUMBER = r"(?:\d+(?:,\d+)*(?:\.\d+)?|\.\d+)"
SCALED_NUMBER = rf"{NUMBER}(?:\s*(?:k|lakhs?|lacs?)(?![A-Za-z]))?"
CURRENCY = r"(?:[₹$€£]|(?<![A-Za-z])(?:INR|USD|EUR|GBP|rupees|Rs)(?![A-Za-z])(?:\.(?!\d))?|/-)"
# Look ahead so one shared marker cannot hide either adjacent amount (300 INR 9999).
AMOUNT_RE = re.compile(
    rf"(?={CURRENCY}\s*(?P<prefix>{SCALED_NUMBER})(?!\d|\.\d|,\d)"
    rf"|(?<![\w.,])(?P<suffix>{SCALED_NUMBER})\s*{CURRENCY})", re.I
)
NUMBER_RE = re.compile(rf"(?<![\d.,]){SCALED_NUMBER}", re.I)
VALUE_RE = re.compile(rf"(?P<number>{NUMBER})(?:\s*(?P<unit>k|lakhs?|lacs?))?", re.I)
URL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[a-z][a-z0-9+.-]*://|//|www\."
    r"|(?:[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@)?(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
    r"[^\s<>]*", re.I
)
TRAILING_PUNCTUATION = ".,;:!)]\"'”’"


def _amounts(text: str) -> list[tuple[Decimal, tuple[int, int]]]:
    amounts = []
    for match in AMOUNT_RE.finditer(text):
        group = "prefix" if match.group("prefix") is not None else "suffix"
        value = VALUE_RE.fullmatch(match.group(group))
        assert value is not None
        number = Decimal(value.group("number").replace(",", ""))
        unit = (value.group("unit") or "").lower()
        scale = 3 if unit == "k" else (5 if unit else 0)
        # Adjust the exponent directly to preserve every digit, regardless of Decimal context precision.
        sign, digits, exponent = number.as_tuple()
        amounts.append((Decimal((sign, digits, exponent + scale)), match.span(group)))
    return amounts


def _approved_amounts(prices: object) -> set[Decimal]:
    if isinstance(prices, dict):
        return {amount for value in prices.values() for amount in _approved_amounts(value)}
    if isinstance(prices, list):
        return {amount for value in prices for amount in _approved_amounts(value)}
    if isinstance(prices, (str, int, float, Decimal)) and not isinstance(prices, bool):
        text = str(prices)
        amounts = _amounts(text)
        if any(not any(start <= match.start() and match.end() <= end for _, (start, end) in amounts)
               for match in NUMBER_RE.finditer(text)):
            logger.warning("Clinic prices contain a number without a currency marker; that number is not approved")
        return {amount for amount, _ in amounts}
    return set()


def check_reply(reply: str, clinic: dict) -> tuple[bool, list[str]]:
    """Return (ok, reasons). ok=False means the reply must NOT be sent."""
    reasons: list[str] = []
    text = (reply or "").strip()
    if not text:
        return False, ["empty reply"]

    if len(text.split()) > MAX_WORDS:
        reasons.append(f"reply longer than {MAX_WORDS} words")

    for label, pattern in FORBIDDEN:
        if pattern.search(text):
            reasons.append(f"forbidden term: {label}")

    if MEDICATION_RE.search(text):
        reasons.append("mentions medication")

    allowed = _approved_amounts(clinic.get("prices", {}))
    for amount, _ in _amounts(text):
        if amount not in allowed:
            reasons.append(f"amount not in clinic config: {amount}")

    links = {clinic.get(key) for key in ("booking_link", "maps_link", "website", "email")
             if isinstance(clinic.get(key), str)}
    for match in URL_RE.finditer(text):
        url = match.group().rstrip(TRAILING_PUNCTUATION)
        if url not in links:
            reasons.append("URL not in clinic allowlist")

    return (not reasons), reasons


def safe_reply(clinic: dict) -> str:
    """Neutral fallback used whenever the LLM reply is missing or rejected."""
    name = clinic.get("clinic_name", "our clinic")
    link = clinic.get("booking_link", "")
    phone = clinic.get("phone", "")
    parts = [f"Thanks for contacting {name}! Our team will reach out to you shortly."]
    if link:
        parts.append(f"You can also book a consultation here: {link}")
    if phone:
        parts.append(f"Or call us on {phone}.")
    return " ".join(parts)
