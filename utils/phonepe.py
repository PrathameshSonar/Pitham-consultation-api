"""
PhonePe Payment Gateway integration using official Python SDK (Standard Checkout v2).

Env vars required:
  PHONEPE_CLIENT_ID      — your client ID
  PHONEPE_CLIENT_SECRET   — your client secret
  PHONEPE_CLIENT_VERSION  — client version (integer, e.g. 1)
  PHONEPE_ENV             — "sandbox" or "production"
"""

import os
from uuid import uuid4

from phonepe.sdk.pg.payments.v2.standard_checkout_client import StandardCheckoutClient
from phonepe.sdk.pg.payments.v2.models.request.standard_checkout_pay_request import StandardCheckoutPayRequest
from phonepe.sdk.pg.env import Env
from phonepe.sdk.pg.common.exceptions import PhonePeException

CLIENT_ID = os.getenv("PHONEPE_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("PHONEPE_CLIENT_SECRET", "")
CLIENT_VERSION = int(os.getenv("PHONEPE_CLIENT_VERSION", "1"))
PHONEPE_ENV = os.getenv("PHONEPE_ENV", "sandbox")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:3000")

# Webhook callback credentials (configured in PhonePe dashboard)
CALLBACK_USERNAME = os.getenv("PHONEPE_CALLBACK_USERNAME", "")
CALLBACK_PASSWORD = os.getenv("PHONEPE_CALLBACK_PASSWORD", "")


class PhonePeError(Exception):
    pass


def _get_client() -> StandardCheckoutClient:
    """Get or create the singleton StandardCheckoutClient."""
    if not CLIENT_ID or not CLIENT_SECRET:
        raise PhonePeError("PhonePe credentials not configured (PHONEPE_CLIENT_ID / PHONEPE_CLIENT_SECRET)")

    env = Env.PRODUCTION if PHONEPE_ENV == "production" else Env.SANDBOX
    return StandardCheckoutClient.get_instance(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        client_version=CLIENT_VERSION,
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

    merchant_order_id = f"PITHAM_{appointment_id}_{uuid4().hex[:8]}"
    amount_paise = int(amount_rupees * 100)
    if amount_paise < 100:
        amount_paise = 100  # PhonePe minimum

    redirect_url = f"{FRONTEND_URL}/appointments/payment-status?txn={merchant_order_id}"

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

    if not CALLBACK_USERNAME or not CALLBACK_PASSWORD:
        raise PhonePeError("Callback credentials not configured")

    try:
        callback_response = client.validate_callback(
            username=CALLBACK_USERNAME,
            password=CALLBACK_PASSWORD,
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
