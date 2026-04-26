"""DB-backed payment gateway secrets with env fallback.

Why both:
    * Existing appointments flow predates this and reads creds from env via
      `config.settings.phonepe`. We don't want to break it.
    * Operator who deploys via Render/Docker may prefer to flip secrets at
      runtime via the admin panel rather than redeploying.

Resolution order (per key):
    1. value stored in `site_settings` (admin-managed)  — preferred
    2. value baked into env (`config.settings.phonepe.*`) — fallback

Both layers can be empty for a not-yet-configured gateway.
"""

from __future__ import annotations

from typing import Dict
from sqlalchemy.orm import Session

from config import settings
from utils.site_settings import get_setting


def _db_or(db: Session, key: str, env_value: str) -> str:
    """Return the DB value if non-empty, else the env value."""
    val = get_setting(db, key)
    if val and val.strip():
        return val.strip()
    return env_value or ""


def get_phonepe_credentials(db: Session) -> Dict[str, str]:
    """Effective PhonePe creds — DB overrides env. Returns empty strings for
    missing fields so callers can decide how to react (e.g. is_configured()
    style checks)."""
    p = settings.phonepe
    return {
        "client_id":         _db_or(db, "payment.phonepe.client_id",         p.client_id or ""),
        "client_secret":     _db_or(db, "payment.phonepe.client_secret",     p.client_secret or ""),
        "client_version":    _db_or(db, "payment.phonepe.client_version",    str(p.client_version or 1)),
        "env":               _db_or(db, "payment.phonepe.env",               p.env or "sandbox"),
        "callback_username": _db_or(db, "payment.phonepe.callback_username", p.callback_username or ""),
        "callback_password": _db_or(db, "payment.phonepe.callback_password", p.callback_password or ""),
    }


def is_phonepe_configured(db: Session) -> bool:
    c = get_phonepe_credentials(db)
    return bool(c["client_id"] and c["client_secret"])


def get_razorpay_credentials(db: Session) -> Dict[str, str]:
    return {
        "key_id":     _db_or(db, "payment.razorpay.key_id",     ""),
        "key_secret": _db_or(db, "payment.razorpay.key_secret", ""),
    }


def is_razorpay_configured(db: Session) -> bool:
    c = get_razorpay_credentials(db)
    return bool(c["key_id"] and c["key_secret"])


def get_gpay_credentials(db: Session) -> Dict[str, str]:
    return {
        "merchant_id": _db_or(db, "payment.gpay.merchant_id", ""),
        "api_key":     _db_or(db, "payment.gpay.api_key",     ""),
    }


def is_gpay_configured(db: Session) -> bool:
    c = get_gpay_credentials(db)
    return bool(c["merchant_id"] and c["api_key"])
