"""Per-event payment gateway dispatch.

Each gateway is a small strategy: take a registration + amount, return where
to send the user (redirect URL, reference id, or "no payment needed").
Adding a new gateway = one function + one branch in `initiate_event_payment`.

Status of each gateway:
    free      — implemented (instant confirm)
    manual    — implemented (admin verifies offline)
    phonepe   — implemented (reuses existing utils/phonepe.py with event txn prefix)
    razorpay  — accepted in config but raises NotImplementedError when invoked
    gpay      — same as razorpay; placeholder for Phase 2
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from uuid import uuid4

from sqlalchemy.orm import Session

from config import settings
from utils import phonepe, razorpay_gw
from utils.payment_secrets import is_phonepe_configured, is_razorpay_configured


class GatewayError(Exception):
    """Raised when a gateway is misconfigured or rejects the request."""


@dataclass
class GatewayInitResult:
    """What `initiate_event_payment` returns.

    requires_payment_action:
        True  — frontend must do something to complete payment:
                  * PhonePe: redirect the browser to `redirect_url`
                  * Razorpay: open the inline checkout popup with `razorpay_order`
                  * manual: nothing (admin verifies offline)
                Registration sits in `pending_payment` until completion.
        False — payment is settled at this moment (free events). Registration
                goes straight to `confirmed`.
    """
    gateway: str
    requires_payment_action: bool
    redirect_url: Optional[str] = None
    reference: Optional[str] = None
    # Razorpay-shaped order info (key_id, order_id, amount, currency, receipt).
    # Frontend reads this to open the Razorpay checkout JS popup.
    razorpay_order: Optional[Dict[str, Any]] = field(default=None)


def initiate_event_payment(
    db: Session,
    *,
    gateway: str,
    registration_id: int,
    amount_rupees: int,
    user_mobile: str,
) -> GatewayInitResult:
    """Dispatch to the configured gateway. Free returns instantly; PhonePe
    returns a hosted-checkout URL the frontend redirects to. Manual returns
    no URL — admin verifies the payment offline and confirms via the
    registrations dashboard."""
    if gateway == "free" or amount_rupees <= 0:
        return GatewayInitResult(gateway="free", requires_payment_action=False)

    if gateway == "manual":
        # Admin verifies offline (cash, bank transfer, etc.); registration sits
        # in pending_payment until the admin marks it paid.
        ref = f"MANUAL_{registration_id}_{uuid4().hex[:8]}"
        return GatewayInitResult(
            gateway="manual",
            requires_payment_action=True,
            redirect_url=None,
            reference=ref,
        )

    if gateway == "phonepe":
        if not is_phonepe_configured(db):
            raise GatewayError("PhonePe is not configured. Add credentials in Settings → Payment Gateways.")
        merchant_order_id = f"SPBSP_EVT_{registration_id}_{uuid4().hex[:8]}"
        amount_paise = max(int(amount_rupees * 100), 100)  # PhonePe minimum
        try:
            redirect_url = (
                f"{settings.core.frontend_url}/dashboard/events/payment-status?txn={merchant_order_id}"
            )
            client = phonepe._get_client()  # underscore-private but stable; OK to reuse here
            from phonepe.sdk.pg.payments.v2.models.request.standard_checkout_pay_request import (
                StandardCheckoutPayRequest,
            )
            pay_request = StandardCheckoutPayRequest.build_request(
                merchant_order_id=merchant_order_id,
                amount=amount_paise,
                redirect_url=redirect_url,
            )
            pay_response = client.pay(pay_request)
            return GatewayInitResult(
                gateway="phonepe",
                requires_payment_action=True,
                redirect_url=pay_response.redirect_url,
                reference=merchant_order_id,
            )
        except phonepe.PhonePeError as e:
            raise GatewayError(str(e))
        except Exception as e:
            raise GatewayError(f"PhonePe payment initiation failed: {e}")

    if gateway == "razorpay":
        if not is_razorpay_configured(db):
            raise GatewayError("Razorpay is not configured. Add credentials in Settings → Payment Gateways.")
        receipt = f"SPBSP_EVT_{registration_id}_{uuid4().hex[:8]}"
        try:
            order = razorpay_gw.create_order(
                db,
                amount_rupees=amount_rupees,
                receipt=receipt,
                notes={"registration_id": str(registration_id)},
            )
        except razorpay_gw.RazorpayError as e:
            raise GatewayError(str(e))
        return GatewayInitResult(
            gateway="razorpay",
            requires_payment_action=True,
            redirect_url=None,
            reference=order["order_id"],
            razorpay_order=order,
        )

    if gateway == "gpay":
        raise GatewayError(
            "Google Pay integration is not yet available. Pick PhonePe, Razorpay, Manual, or Free."
        )

    raise GatewayError(f"Unknown gateway: {gateway}")


def check_event_payment(merchant_order_id: str) -> dict:
    """Poll an in-flight PhonePe order for an event registration. Mirrors the
    appointment-side helper so the frontend payment-status page can hit one
    endpoint and decide what to do next."""
    return phonepe.check_payment_status(merchant_order_id)
