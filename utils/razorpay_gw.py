"""Razorpay Standard Checkout integration.

Payment flow (different from PhonePe!):
    1. Backend creates an Order via Razorpay's REST API → returns order_id
    2. Backend hands the order_id + key_id back to the frontend
    3. Frontend opens the Razorpay checkout JS popup with that data
    4. User pays inside the popup
    5. Razorpay returns { razorpay_order_id, razorpay_payment_id, razorpay_signature }
    6. Frontend POSTs that triple to /events/registrations/{id}/razorpay-verify
    7. Backend verifies the HMAC signature against key_secret → confirms payment

We never see the user's card. Razorpay's checkout JS handles UI + capture.

Credentials:
    Resolved through utils/payment_secrets.get_razorpay_credentials() — DB
    overrides env. The SDK is only imported lazily so a deployment without
    razorpay configured doesn't fail to boot.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Dict, Optional

from sqlalchemy.orm import Session

from utils.payment_secrets import get_razorpay_credentials, is_razorpay_configured


class RazorpayError(Exception):
    pass


def _client(db: Session):
    """Lazily build a razorpay.Client. Raises RazorpayError when the SDK
    isn't installed or credentials are missing — callers can surface that
    to the admin without crashing the request handler."""
    creds = get_razorpay_credentials(db)
    if not creds["key_id"] or not creds["key_secret"]:
        raise RazorpayError(
            "Razorpay is not configured. Add credentials in Settings → Payment Gateways."
        )
    try:
        import razorpay  # type: ignore
    except ImportError:
        raise RazorpayError(
            "razorpay SDK not installed. Run `pip install razorpay` and redeploy."
        )
    return razorpay.Client(auth=(creds["key_id"], creds["key_secret"]))


def create_order(
    db: Session,
    *,
    amount_rupees: int,
    receipt: str,
    notes: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Create a Razorpay Order. Returns the order info the frontend needs to
    open the checkout popup: {key_id, order_id, amount, currency, name}.

    `amount_rupees` is in whole rupees; Razorpay expects paise so we *100.
    Razorpay's minimum is 100 paise (₹1) — we clamp below that to avoid a
    confusing 400 from their API.
    """
    if amount_rupees <= 0:
        raise RazorpayError("Razorpay cannot process a zero-amount order.")
    client = _client(db)
    creds = get_razorpay_credentials(db)
    amount_paise = max(int(amount_rupees * 100), 100)
    try:
        order = client.order.create(
            data={
                "amount": amount_paise,
                "currency": "INR",
                "receipt": receipt[:40],   # Razorpay caps receipt at 40 chars
                "notes": notes or {},
                "payment_capture": 1,      # auto-capture once authorised
            }
        )
    except Exception as e:
        raise RazorpayError(f"Razorpay order creation failed: {e}")
    return {
        "key_id": creds["key_id"],
        "order_id": order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "receipt": order.get("receipt", receipt),
    }


def verify_payment_signature(
    db: Session,
    *,
    razorpay_order_id: str,
    razorpay_payment_id: str,
    razorpay_signature: str,
) -> bool:
    """Verify the HMAC-SHA256 signature Razorpay returns after a successful
    payment. The signed payload is `order_id + "|" + payment_id` keyed with
    `key_secret`. Returns True only when the signature matches.

    Done locally with hmac.compare_digest so we don't depend on the SDK's
    built-in helper (and so this still works if the SDK refuses to import
    for any reason)."""
    creds = get_razorpay_credentials(db)
    secret = creds["key_secret"]
    if not secret:
        raise RazorpayError("Razorpay key_secret missing; cannot verify signature.")

    payload = f"{razorpay_order_id}|{razorpay_payment_id}".encode("utf-8")
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, razorpay_signature or "")


__all__ = [
    "RazorpayError",
    "create_order",
    "verify_payment_signature",
    "is_razorpay_configured",
]
