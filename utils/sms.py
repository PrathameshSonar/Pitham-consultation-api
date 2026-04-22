"""SMS — provider-agnostic webhook sender.

Wire any SMS provider by pointing SMS_PROVIDER_URL at an endpoint that accepts
a JSON POST of `{to, message, sender_id?}`. If not configured, sends are
logged as stubs and silently skipped.

Works with MSG91, Gupshup, Kaleyra, TextLocal, or your own Twilio relay —
auth is supplied via SMS_AUTH_HEADER ("Header-Name: value" format).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

import requests

from config import settings

logger = logging.getLogger("pitham.sms")
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="sms-")


def _send_sync(to: str, body: str) -> None:
    cfg = settings.sms
    payload = {"to": to, "message": body[:1600]}  # 10 SMS concat max on most providers
    if cfg.sender_id:
        payload["sender_id"] = cfg.sender_id
    try:
        r = requests.post(cfg.provider_url, json=payload, headers=cfg.header_dict(), timeout=10)
        if r.status_code >= 400:
            logger.error("SMS send failed %s to=%s: %s", r.status_code, to, r.text[:300])
        else:
            logger.info("SMS sent to=%s", to)
    except requests.RequestException as e:
        logger.error("SMS send error to=%s: %s", to, e)


def send_sms(to: str, body: str) -> None:
    """Fire-and-forget SMS. Returns immediately."""
    if not to:
        return
    if not settings.sms.is_configured():
        logger.info("SMS (stub) to=%s body=%s", to, body[:80])
        return
    _pool.submit(_send_sync, to, body)
