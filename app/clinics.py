"""Load per-clinic configuration from clinics/<clinic_id>.yaml."""
from __future__ import annotations

import re
import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationInfo, model_validator

from app.config import get_settings

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
FILENAME_RE = re.compile(r"[a-z0-9_-]{1,64}")
logger = logging.getLogger(__name__)


def _placeholder_fields(value: object, path: str = "") -> list[str]:
    if isinstance(value, str):
        return [path] if value.strip().upper().startswith("REPLACE") else []
    if isinstance(value, dict):
        return [field for key, item in value.items()
                for field in _placeholder_fields(item, f"{path}.{key}".lstrip("."))]
    if isinstance(value, list):
        return [field for index, item in enumerate(value)
                for field in _placeholder_fields(item, f"{path}[{index}]")]
    return []


class ClinicConfig(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    clinic_name: str = Field(min_length=1)
    booking_link: HttpUrl
    phone: str = Field(min_length=1)
    location: str = Field(min_length=1)
    timings: str = Field(min_length=1)
    prices: dict[str, str]
    mode: Literal["shadow", "auto"]
    clinic_id: str | None = None
    maps_link: HttpUrl | None = None
    owner_telegram_chat_id: str | int | None = None
    owner_email: str | None = None
    doctors: list[str] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    payment_options: str | None = None
    languages: str | list[str] | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_placeholders(cls, data: object, info: ValidationInfo) -> object:
        if isinstance(data, dict) and (
            str(data.get("mode", "")).strip() == "auto" or (info.context or {}).get("production")
        ):
            placeholders = _placeholder_fields(data)
            if placeholders:
                raise ValueError("Replace placeholder values in: " + ", ".join(placeholders))
        return data


class ClinicNotFound(Exception):
    pass


class ClinicConfigError(ValueError):
    pass


def _load_path(path: Path) -> dict:
    try:
        if not FILENAME_RE.fullmatch(path.stem):
            raise ValueError("filename stem must match ^[a-z0-9_-]{1,64}$ (lowercase)")
        with path.open(encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ValueError("expected a YAML mapping of clinic fields")
        data["clinic_id"] = path.stem.lower()
        config = ClinicConfig.model_validate(
            data, context={"production": get_settings().app_env == "production"})
        return config.model_dump(mode="json", exclude_none=True)
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise ClinicConfigError(f"Invalid clinic config {path.name}: {exc}") from exc


def validate_clinics() -> list[dict]:
    configs, errors = [], []
    for path in sorted(get_settings().clinics_dir.glob("*.yaml")):
        try:
            configs.append(_load_path(path))
        except ClinicConfigError as exc:
            errors.append(str(exc))
    if errors and get_settings().app_env == "production":
        raise ClinicConfigError("\n".join(errors))
    for error in errors:
        logger.warning("%s", error)
    return configs


def load_clinic(clinic_id: str) -> dict:
    if not ID_RE.match(clinic_id or ""):
        raise ClinicNotFound(f"invalid clinic id: {clinic_id!r}")
    clinic_id = clinic_id.lower()
    path = get_settings().clinics_dir / f"{clinic_id}.yaml"
    if not path.is_file():
        raise ClinicNotFound(clinic_id)
    return _load_path(path)
