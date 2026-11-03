"""Pydantic schemas for the webhook payload and the LLM result."""
from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
INTENTS = {"booking", "pricing", "treatment_info", "location_timing", "complaint", "other"}


def normalize_phone(raw: str) -> str:
    raw = raw.strip()
    digits = re.sub(r"\D", "", raw)
    return ("+" if raw.startswith("+") else "") + digits


class LeadIn(BaseModel):
    clinic_id: str = Field(default="demo_clinic", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    phone: str
    email: Optional[str] = None
    message: str = Field(min_length=1, max_length=2000)
    service: Optional[str] = Field(default=None, max_length=100)

    @field_validator("clinic_id")
    @classmethod
    def _clinic_id(cls, v: str) -> str:
        return v.lower()

    @field_validator("name", "message", mode="before")
    @classmethod
    def _strip(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("phone", mode="before")
    @classmethod
    def _phone(cls, v):
        if not isinstance(v, str):
            raise ValueError("phone must be a string")
        cleaned = normalize_phone(v)
        if len(re.sub(r"\D", "", cleaned)) < 7:
            raise ValueError("phone number too short")
        return cleaned

    @field_validator("email", mode="before")
    @classmethod
    def _email(cls, v):
        if v is None:
            return None
        v = str(v)
        if not v:
            return None
        if re.search(r"[\s,;<>]", v) or v.count("@") != 1 or not EMAIL_RE.fullmatch(v):
            raise ValueError("invalid email")
        return v.lower()


class QualificationResult(BaseModel):
    llm_failed: bool = False
    language: str = "unknown"
    intent: str = "other"
    score: Literal["hot", "warm", "cold", "spam"]
    urgent: bool = False
    needs_human: bool = False
    reason: str = ""
    reply: str = ""

    @field_validator("score", mode="before")
    @classmethod
    def _score(cls, v):
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("intent", mode="before")
    @classmethod
    def _intent(cls, v):
        v = v.strip().lower() if isinstance(v, str) else "other"
        return v if v in INTENTS else "other"

    @model_validator(mode="after")
    def _urgent_needs_human(self):
        if self.urgent:
            self.needs_human = True
        return self
