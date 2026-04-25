"""Single source of truth for every external integration credential.

To move to production: edit backend/.env only — no code changes needed.
Each integration has an `is_configured()` check; if it returns False the
corresponding sender/driver will silently skip (and log) rather than crash.

See backend/.env.example for the full list of variables.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env before any field-default calls os.getenv()
load_dotenv(Path(__file__).resolve().parent / ".env")

logger = logging.getLogger("pitham.config")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# Core
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CoreConfig:
    env: str = field(default_factory=lambda: _env("ENV", "development"))
    secret_key: str = field(default_factory=lambda: _env("SECRET_KEY", ""))
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", "sqlite:///./pitham.db"))
    frontend_url: str = field(default_factory=lambda: _env("FRONTEND_URL", "http://localhost:3000"))
    sql_echo: bool = field(default_factory=lambda: _env_bool("SQL_ECHO"))
    admin_rate_limit_per_min: int = field(default_factory=lambda: _env_int("ADMIN_RATE_LIMIT_PER_MIN", 120))

    @property
    def is_production(self) -> bool:
        return self.env == "production"


# ─────────────────────────────────────────────────────────────────────────────
# Email (SMTP)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EmailConfig:
    username: str = field(default_factory=lambda: _env("MAIL_USERNAME", ""))
    password: str = field(default_factory=lambda: _env("MAIL_PASSWORD", ""))
    from_addr: str = field(default_factory=lambda: _env("MAIL_FROM", "") or _env("MAIL_USERNAME", ""))
    server: str = field(default_factory=lambda: _env("MAIL_SERVER", "smtp.gmail.com"))
    port: int = field(default_factory=lambda: _env_int("MAIL_PORT", 587))

    def is_configured(self) -> bool:
        return bool(self.username and self.password)


# ─────────────────────────────────────────────────────────────────────────────
# WhatsApp (Meta Cloud API)
#   Get credentials at developers.facebook.com → WhatsApp → API Setup.
#   For production, approved message templates are required outside the 24-hour
#   customer-service window. In sandbox, free-form text works to test numbers.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WhatsAppConfig:
    access_token: str = field(default_factory=lambda: _env("WHATSAPP_ACCESS_TOKEN", ""))
    phone_number_id: str = field(default_factory=lambda: _env("WHATSAPP_PHONE_NUMBER_ID", ""))
    api_version: str = field(default_factory=lambda: _env("WHATSAPP_API_VERSION", "v22.0"))
    # Optional — name of an approved template for booking confirmations (for 24h+)
    default_template: str = field(default_factory=lambda: _env("WHATSAPP_DEFAULT_TEMPLATE", ""))
    default_template_lang: str = field(default_factory=lambda: _env("WHATSAPP_DEFAULT_TEMPLATE_LANG", "en"))

    def is_configured(self) -> bool:
        return bool(self.access_token and self.phone_number_id)

    @property
    def send_url(self) -> str:
        return f"https://graph.facebook.com/{self.api_version}/{self.phone_number_id}/messages"


# ─────────────────────────────────────────────────────────────────────────────
# SMS (provider-agnostic webhook)
#   Configure any provider (MSG91 / Twilio / Gupshup / your own relay) as long
#   as it accepts a JSON POST {"to": "+91...", "message": "..."}.
#   Set SMS_AUTH_HEADER if the provider needs an auth header, e.g.:
#     SMS_AUTH_HEADER='Authorization: Bearer xyz'
#     SMS_AUTH_HEADER='authkey: abc123'
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SmsConfig:
    provider_url: str = field(default_factory=lambda: _env("SMS_PROVIDER_URL", ""))
    auth_header: str = field(default_factory=lambda: _env("SMS_AUTH_HEADER", ""))
    sender_id: str = field(default_factory=lambda: _env("SMS_SENDER_ID", ""))

    def is_configured(self) -> bool:
        return bool(self.provider_url)

    def header_dict(self) -> dict[str, str]:
        """Split a SMS_AUTH_HEADER like 'Name: value' into a {name: value} dict."""
        headers = {"Content-Type": "application/json"}
        if self.auth_header and ":" in self.auth_header:
            name, _, value = self.auth_header.partition(":")
            headers[name.strip()] = value.strip()
        return headers


# ─────────────────────────────────────────────────────────────────────────────
# Google Sign-In (OAuth ID-token verification)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GoogleConfig:
    client_id: str = field(default_factory=lambda: _env("GOOGLE_CLIENT_ID", ""))

    def is_configured(self) -> bool:
        return bool(self.client_id)


# ─────────────────────────────────────────────────────────────────────────────
# PhonePe (Payments)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PhonePeConfig:
    client_id: str = field(default_factory=lambda: _env("PHONEPE_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: _env("PHONEPE_CLIENT_SECRET", ""))
    client_version: int = field(default_factory=lambda: _env_int("PHONEPE_CLIENT_VERSION", 1))
    env: str = field(default_factory=lambda: _env("PHONEPE_ENV", "sandbox"))
    callback_username: str = field(default_factory=lambda: _env("PHONEPE_CALLBACK_USERNAME", ""))
    callback_password: str = field(default_factory=lambda: _env("PHONEPE_CALLBACK_PASSWORD", ""))

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret)


# ─────────────────────────────────────────────────────────────────────────────
# Zoom
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ZoomConfig:
    account_id: str = field(default_factory=lambda: _env("ZOOM_ACCOUNT_ID", ""))
    client_id: str = field(default_factory=lambda: _env("ZOOM_CLIENT_ID", ""))
    client_secret: str = field(default_factory=lambda: _env("ZOOM_CLIENT_SECRET", ""))
    timezone: str = field(default_factory=lambda: _env("ZOOM_TIMEZONE", "Asia/Kolkata"))

    def is_configured(self) -> bool:
        return bool(self.account_id and self.client_id and self.client_secret)


# ─────────────────────────────────────────────────────────────────────────────
# Sentry
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SentryConfig:
    dsn: str = field(default_factory=lambda: _env("SENTRY_DSN", ""))
    traces_rate: float = field(default_factory=lambda: float(_env("SENTRY_TRACES_RATE", "0.1") or "0.1"))

    def is_configured(self) -> bool:
        return bool(self.dsn)


# ─────────────────────────────────────────────────────────────────────────────
# Storage
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StorageConfig:
    driver: str = field(default_factory=lambda: _env("STORAGE_DRIVER", "local").lower())
    local_upload_dir: str = field(default_factory=lambda: _env("LOCAL_UPLOAD_DIR", "uploads"))
    # S3 / S3-compatible (R2, Spaces, Backblaze B2)
    s3_bucket: str = field(default_factory=lambda: _env("S3_BUCKET", ""))
    s3_region: str = field(default_factory=lambda: _env("S3_REGION", "auto"))
    s3_endpoint_url: str = field(default_factory=lambda: _env("S3_ENDPOINT_URL", ""))   # blank = AWS default
    s3_access_key: str = field(default_factory=lambda: _env("S3_ACCESS_KEY_ID", ""))
    s3_secret_key: str = field(default_factory=lambda: _env("S3_SECRET_ACCESS_KEY", ""))
    s3_public_base_url: str = field(default_factory=lambda: _env("S3_PUBLIC_BASE_URL", ""))   # CDN or bucket URL


# ─────────────────────────────────────────────────────────────────────────────
# Singleton — import this everywhere
#    from config import settings
#    if settings.whatsapp.is_configured(): ...
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    core: CoreConfig = field(default_factory=CoreConfig)
    email: EmailConfig = field(default_factory=EmailConfig)
    whatsapp: WhatsAppConfig = field(default_factory=WhatsAppConfig)
    sms: SmsConfig = field(default_factory=SmsConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)
    phonepe: PhonePeConfig = field(default_factory=PhonePeConfig)
    zoom: ZoomConfig = field(default_factory=ZoomConfig)
    sentry: SentryConfig = field(default_factory=SentryConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)

    def summary(self) -> dict[str, bool]:
        """Useful on startup: log which integrations are live so the deployer can see at a glance."""
        return {
            "email": self.email.is_configured(),
            "whatsapp": self.whatsapp.is_configured(),
            "sms": self.sms.is_configured(),
            "google": self.google.is_configured(),
            "phonepe": self.phonepe.is_configured(),
            "zoom": self.zoom.is_configured(),
            "sentry": self.sentry.is_configured(),
        }


settings = Settings()
