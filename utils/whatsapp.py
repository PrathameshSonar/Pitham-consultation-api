"""WhatsApp notifications via Meta's WhatsApp Cloud API.

Centrally configured in backend/config.py. If credentials aren't set, sends are
logged as stubs and silently skipped — so dev environments can run without
blowing up.

Prod notes:
  - Outside the 24-hour customer-service window, only APPROVED templates can
    be sent. For booking / OTP flows, register templates at
    business.facebook.com → WhatsApp Manager → Message Templates.
  - Set WHATSAPP_DEFAULT_TEMPLATE + WHATSAPP_DEFAULT_TEMPLATE_LANG in .env;
    this module will use template sends when that's configured, and fall back
    to free-form text otherwise (which works in sandbox + 24h window).
"""

from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Sequence

import requests

from config import settings

logger = logging.getLogger("pitham.whatsapp")

# Share a pool with emails would be fine, but keeping separate means a flaky
# provider doesn't stall other notification channels.
_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="whatsapp-")


def _normalize(phone: str) -> str:
    """Strip spaces, dashes, and the leading '+'; Meta's API wants E.164 digits only."""
    if not phone:
        return ""
    cleaned = re.sub(r"[^\d+]", "", phone)
    return cleaned.lstrip("+")


def _send_text_sync(to: str, body: str) -> None:
    cfg = settings.whatsapp
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize(to),
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},  # Meta's hard limit
    }
    try:
        r = requests.post(
            cfg.send_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {cfg.access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if r.status_code >= 400:
            logger.error("WhatsApp text send failed %s to=%s: %s", r.status_code, to, r.text[:300])
        else:
            logger.info("WhatsApp text sent to=%s", to)
    except requests.RequestException as e:
        logger.error("WhatsApp text send error to=%s: %s", to, e)


def _send_template_sync(to: str, template_name: str, lang: str, params: Sequence[str]) -> None:
    cfg = settings.whatsapp
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": _normalize(to),
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": lang},
        },
    }
    if params:
        payload["template"]["components"] = [{
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in params],
        }]
    try:
        r = requests.post(
            cfg.send_url,
            json=payload,
            headers={
                "Authorization": f"Bearer {cfg.access_token}",
                "Content-Type": "application/json",
            },
            timeout=10,
        )
        if r.status_code >= 400:
            logger.error(
                "WhatsApp template '%s' send failed %s to=%s: %s",
                template_name, r.status_code, to, r.text[:300],
            )
        else:
            logger.info("WhatsApp template '%s' sent to=%s", template_name, to)
    except requests.RequestException as e:
        logger.error("WhatsApp template send error to=%s: %s", to, e)


def send_whatsapp(to: str, body: str) -> None:
    """Fire-and-forget WhatsApp message. Returns immediately.

    Behaviour:
      - Not configured → logs stub and returns (safe for dev).
      - No recipient → returns.
      - WHATSAPP_DEFAULT_TEMPLATE set → sends as a template message with `body`
        as the single substitution variable (works outside 24h window).
      - Otherwise → sends free-form text (works only in sandbox / within 24h).
    """
    cfg = settings.whatsapp
    if not to:
        return
    if not cfg.is_configured():
        logger.info("WhatsApp (stub) to=%s body=%s", to, body[:80])
        return

    if cfg.default_template:
        _pool.submit(_send_template_sync, to, cfg.default_template, cfg.default_template_lang, [body])
    else:
        _pool.submit(_send_text_sync, to, body)


def send_whatsapp_template(to: str, template_name: str, params: Sequence[str], lang: str | None = None) -> None:
    """Explicit template send — use this when different notifications should use
    different approved templates (e.g. booking_confirmed vs otp_reset)."""
    cfg = settings.whatsapp
    if not to:
        return
    if not cfg.is_configured():
        logger.info("WhatsApp template (stub) to=%s template=%s params=%s", to, template_name, params)
        return
    _pool.submit(_send_template_sync, to, template_name, lang or cfg.default_template_lang, list(params))
