"""Application settings, read from environment variables on every call."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_env: str
    webhook_secret: str
    admin_secret: str
    db_path: str
    clinics_dir: Path
    prompts_dir: Path
    llm_provider: str
    llm_api_key: str
    llm_model: str
    llm_timeout: float
    telegram_bot_token: str
    admin_telegram_chat_id: str
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_from: str
    smtp_use_tls: bool
    enable_scheduler: bool
    enable_recovery: bool
    lead_workers: int
    followup_interval_minutes: int
    followup_delay_hours: int
    duplicate_window_hours: int


def get_settings() -> Settings:
    provider = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
    # Defaults are only a convenience; set LLM_MODEL explicitly after checking current model names.
    default_model = {"anthropic": "claude-sonnet-5-5", "openai": "gpt-4o-mini"}.get(provider, "")
    return Settings(
        app_env=os.getenv("APP_ENV", "development"),
        webhook_secret=os.getenv("WEBHOOK_SECRET", ""),
        admin_secret=os.getenv("ADMIN_SECRET", ""),
        db_path=os.getenv("DB_PATH", str(BASE_DIR / "leads.db")),
        clinics_dir=Path(os.getenv("CLINICS_DIR", str(BASE_DIR / "clinics"))),
        prompts_dir=BASE_DIR / "app" / "prompts",
        llm_provider=provider,
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        llm_model=os.getenv("LLM_MODEL") or default_model,
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "20")),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        admin_telegram_chat_id=os.getenv("ADMIN_TELEGRAM_CHAT_ID", ""),
        smtp_host=os.getenv("SMTP_HOST", ""),
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_user=os.getenv("SMTP_USER", ""),
        smtp_password=os.getenv("SMTP_PASSWORD", ""),
        smtp_from=os.getenv("SMTP_FROM", ""),
        smtp_use_tls=_flag("SMTP_USE_TLS", "1"),
        enable_scheduler=_flag("ENABLE_SCHEDULER", "0"),
        enable_recovery=_flag("ENABLE_RECOVERY", "1"),
        lead_workers=max(1, int(os.getenv("LEAD_WORKERS", "4"))),
        followup_interval_minutes=int(os.getenv("FOLLOWUP_INTERVAL_MINUTES", "15")),
        followup_delay_hours=int(os.getenv("FOLLOWUP_DELAY_HOURS", "24")),
        duplicate_window_hours=int(os.getenv("DUPLICATE_WINDOW_HOURS", "24")),
    )


def missing_required(s: Settings) -> list[str]:
    """Names of env vars that must be set for production use."""
    required = {
        "WEBHOOK_SECRET": s.webhook_secret,
        "ADMIN_SECRET": s.admin_secret,
        "LLM_API_KEY": s.llm_api_key,
        "TELEGRAM_BOT_TOKEN": s.telegram_bot_token,
        "ADMIN_TELEGRAM_CHAT_ID": s.admin_telegram_chat_id,
    }
    return [name for name, value in required.items() if not value]
