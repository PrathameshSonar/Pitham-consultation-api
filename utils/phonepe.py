"""
PhonePe Payment Gateway integration using official Python SDK (Standard Checkout v2).

All credentials are read from backend/config.py (→ .env).
"""

from uuid import uuid4

from phonepe.sdk.pg.payments.v2.standard_checkout_client import StandardCheckoutClient
from phonepe.sdk.pg.payments.v2.models.request.standard_checkout_pay_request import StandardCheckoutPayRequest
from phonepe.sdk.pg.env import Env
from phonepe.sdk.pg.common.exceptions import PhonePeException

from config import settings


class PhonePeError(Exception):
    pass


def _get_client() -> StandardCheckoutClient:
    """Get or create the singleton StandardCheckoutClient."""
    cfg = settings.phonepe
    if not cfg.is_configured():
        raise PhonePeError("PhonePe credentials not configured (PHONEPE_CLIENT_ID / PHONEPE_CLIENT_SECRET)")

    env = Env.PRODUCTION if cfg.env == "production" else Env.SANDBOX
    return StandardCheckoutClient.get_instance(
        client_id=cfg.client_id,
        client_secret=cfg.client_secret,
        client_version=cfg.client_version,
        env=env,
        should_publish_events=False,
    )


def initiate_payment(
    appointment_id: int,
    amount_rupees: float,
    user_mobile: str,
) -> dict:
    """
    Create a PhonePe Standard Checkout payment.
    Returns { "redirect_url": "...", "merchant_order_id": "..." }
    """
    client = _get_client()

    merchant_order_id = f"SPBSP_{appointment_id}_{uuid4().hex[:8]}"
    amount_paise = int(amount_rupees * 100)
    if amount_paise < 100:
        amount_paise = 100  # PhonePe minimum

    redirect_url = f"{settings.core.frontend_url}/appointments/payment-status?txn={merchant_order_id}"

    try:
        pay_request = StandardCheckoutPayRequest.build_request(
            merchant_order_id=merchant_order_id,
            amount=amount_paise,
            redirect_url=redirect_url,
        )
        pay_response = client.pay(pay_request)

        return {
            "redirect_url": pay_response.redirect_url,
            "merchant_order_id": merchant_order_id,
        }
    except PhonePeException as e:
        raise PhonePeError(f"PhonePe payment initiation failed: {e.message}")


def check_payment_status(merchant_order_id: str) -> dict:
    """
    Check status of a PhonePe order.
    Returns { "success": bool, "state": "COMPLETED"|"PENDING"|"FAILED" }
    """
    client = _get_client()

    try:
        response = client.get_order_status(merchant_order_id)
        state = response.state  # "COMPLETED", "PENDING", "FAILED"

        return {
            "success": state == "COMPLETED",
            "state": state,
            "merchant_order_id": merchant_order_id,
        }
    except PhonePeException as e:
        raise PhonePeError(f"PhonePe status check failed: {e.message}")


def validate_callback(authorization_header: str, callback_body: str) -> dict:
    """
    Validate and parse a PhonePe webhook callback.
    Returns { "event": "...", "state": "...", "merchant_order_id": "..." }
    """
    client = _get_client()
    cfg = settings.phonepe

    if not cfg.callback_username or not cfg.callback_password:
        raise PhonePeError("Callback credentials not configured")

    try:
        callback_response = client.validate_callback(
            username=cfg.callback_username,
            password=cfg.callback_password,
            callback_header_data=authorization_header,
            callback_response_data=callback_body,
        )

        event = callback_response.event
        payload = callback_response.payload

        return {
            "event": event,
            "state": getattr(payload, "state", "UNKNOWN"),
            "merchant_order_id": getattr(payload, "original_merchant_order_id", ""),
        }
    except PhonePeException as e:
        raise PhonePeError(f"Callback validation failed: {e.message}")
