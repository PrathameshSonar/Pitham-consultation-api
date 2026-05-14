import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session
from pydantic import BaseModel

from database import get_db
import models
from utils.auth import get_current_user
from utils.phonepe import (
    initiate_payment, check_payment_status, validate_callback, PhonePeError,
)
from utils.pdf_receipt import generate_receipt
from utils.email import send_booking_confirmation
from utils.site_settings import get_setting
from utils import razorpay_gw

logger = logging.getLogger("pitham.payments")

router = APIRouter(prefix="/payments", tags=["payments"])


class InitiatePaymentRequest(BaseModel):
    appointment_id: int
    # `amount` is accepted from the client for backwards-compat with older
    # frontends but is IGNORED — the server pulls the fee from
    # consultation_fee site_setting. Sending a tampered amount used to let
    # users pay ₹1 for a ₹2000 consultation.
    amount: float | None = None


class RazorpayInitAppointmentRequest(BaseModel):
    appointment_id: int


class RazorpayVerifyAppointmentRequest(BaseModel):
    appointment_id: int
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


class InitiatePaymentResponse(BaseModel):
    redirect_url: str
    transaction_id: str


class PaymentStatusResponse(BaseModel):
    success: bool
    state: str
    transaction_id: str


def _set_appt_order_id(appt: models.Appointment, order_id: str) -> None:
    """Stamp the gateway order_id on an appointment at init time. Mirrors to
    payment_reference (legacy)."""
    appt.payment_order_id = order_id
    appt.payment_reference = order_id


def _set_appt_payment_id(appt: models.Appointment, payment_id: str) -> None:
    """Stamp the gateway payment_id on an appointment after success. Mirrors
    to payment_reference (legacy). Does not clear payment_order_id."""
    appt.payment_id = payment_id
    appt.payment_reference = payment_id


def _mark_paid_and_generate_receipt(appt: models.Appointment, db: Session):
    """Mark appointment as paid and generate the booking receipt PDF."""
    appt.payment_status = "paid"
    appt.status = models.AppointmentStatus.payment_verified

    # Get current consultation fee from site settings (with proper default fallback)
    fee = get_setting(db, "consultation_fee")

    # Use the T&C snapshot from booking time (not current settings)
    terms_html = appt.agreed_terms or ""

    # Generate receipt PDF
    try:
        receipt_path = generate_receipt(
            appointment_id=appt.id,
            name=appt.name,
            email=appt.email,
            mobile=appt.mobile,
            dob=appt.dob,
            tob=appt.tob,
            birth_place=appt.birth_place,
            problem=appt.problem,
            payment_reference=appt.payment_reference or "",
            fee=fee,
            booked_on=appt.created_at.strftime("%d %B %Y") if appt.created_at else "",
            consultation_terms=terms_html,
        )
        appt.receipt_path = receipt_path
    except Exception:
        pass  # Don't fail the payment flow if PDF generation fails

    db.commit()

    # Send booking confirmation email with receipt attached
    try:
        send_booking_confirmation(
            to=appt.email,
            mobile=appt.mobile,
            name=appt.name,
            booking_id=appt.id,
            fee=fee,
            receipt_path=appt.receipt_path or "",
        )
    except Exception:
        pass


@router.post("/phonepe/initiate", response_model=InitiatePaymentResponse)
def phonepe_initiate(
    data: InitiatePaymentRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.id == data.appointment_id,
            models.Appointment.user_id == user.id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    if appt.payment_status == "paid":
        raise HTTPException(status_code=400, detail="Payment already completed")
    # If admin (or user) cancelled the booking, payment is not allowed.
    if appt.status in ("cancelled", "completed"):
        raise HTTPException(
            status_code=400,
            detail="This appointment has been cancelled and can no longer be paid for.",
        )

    # Server-side fee resolution. `data.amount` from the client is ignored —
    # if the user crafts a request asking for a ₹1 order, we still tell
    # PhonePe the real consultation fee.
    try:
        fee_rupees = int(get_setting(db, "consultation_fee") or 0)
    except (TypeError, ValueError):
        fee_rupees = 0
    if fee_rupees <= 0:
        raise HTTPException(
            status_code=400,
            detail="Consultation fee is not configured. Contact the administrator.",
        )

    try:
        result = initiate_payment(
            appointment_id=appt.id,
            amount_rupees=fee_rupees,
            user_mobile=user.mobile,
        )
    except PhonePeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    _set_appt_order_id(appt, result["merchant_order_id"])
    appt.payment_id = None  # fresh init clears any stale post-success ID
    appt.status = models.AppointmentStatus.payment_pending
    db.commit()

    return {
        "redirect_url": result["redirect_url"],
        "transaction_id": result["merchant_order_id"],
    }


@router.get("/phonepe/status/{transaction_id}", response_model=PaymentStatusResponse)
def phonepe_status(
    transaction_id: str,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = (
        db.query(models.Appointment)
        .filter(
            (models.Appointment.payment_order_id == transaction_id)
            | (models.Appointment.payment_reference == transaction_id),
            models.Appointment.user_id == user.id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Transaction not found")

    try:
        result = check_payment_status(transaction_id)
    except PhonePeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    if result["success"] and appt.payment_status != "paid":
        # Amount-tamper defence — confirm PhonePe charged at least the
        # configured consultation fee before flipping to paid.
        try:
            expected_paise = int(get_setting(db, "consultation_fee") or 0) * 100
        except (TypeError, ValueError):
            expected_paise = 0
        actual_paise = int(result.get("amount_paise") or 0)
        if expected_paise > 0 and actual_paise > 0 and actual_paise < expected_paise:
            logger.error(
                "phonepe appt amount mismatch txn=%s expected_paise=%s actual_paise=%s",
                transaction_id, expected_paise, actual_paise,
            )
            return {
                "success": False,
                "state": "AMOUNT_MISMATCH",
                "transaction_id": transaction_id,
            }
        # PhonePe doesn't expose a separate payment_id — treat the merchant
        # order id as both. payment_order_id is already set from init.
        _set_appt_payment_id(appt, transaction_id)
        _mark_paid_and_generate_receipt(appt, db)

    return {
        "success": result["success"],
        "state": result["state"],
        "transaction_id": transaction_id,
    }


@router.post("/phonepe/callback")
async def phonepe_callback(
    request: Request,
    db: Session = Depends(get_db),
):
    """PhonePe webhook. Validates Basic-auth signature, then verifies the
    paid amount matches our configured consultation_fee before flipping the
    appointment to paid.

    Error policy:
        - PhonePeError (auth/parse failure) → 401 so PhonePe retries AND so
          a misconfigured callback cred is loud, not silent.
        - Any other exception → log and return 500 so we see it in Sentry,
          rather than swallowing and lying with `{"status":"ok"}`.
    """
    authorization = request.headers.get("Authorization", "")
    body = await request.body()
    body_str = body.decode("utf-8")

    try:
        result = validate_callback(authorization, body_str)
    except PhonePeError as e:
        # Bad Basic-auth or malformed payload. Returning 401 makes PhonePe
        # retry on transient mismatches AND makes the misconfig visible in
        # the logs of whoever is monitoring the webhook.
        logger.warning("phonepe callback rejected: %s", e)
        raise HTTPException(status_code=401, detail="Invalid callback signature")

    if not (result["event"] == "checkout.order.completed" and result["state"] == "COMPLETED"):
        # Not a success notification — could be PENDING/FAILED. Acknowledge
        # so PhonePe stops retrying, but do NOT mark anything paid.
        return {"status": "ok"}

    order_id = result["merchant_order_id"]
    if not order_id:
        logger.warning("phonepe callback missing merchant_order_id")
        return {"status": "ok"}

    actual_paise = int(result.get("amount_paise") or 0)

    try:
        # Try the appointment table first (consultation flow).
        appt = (
            db.query(models.Appointment)
            .filter(
                (models.Appointment.payment_order_id == order_id)
                | (models.Appointment.payment_reference == order_id)
            )
            .first()
        )
        if not appt:
            # Not an appointment — try the event registrations table. The
            # PhonePe merchant callback URL is global per merchant, so a
            # single endpoint receives notifications for both products. If
            # we don't handle event regs here, a user who closes the tab
            # before polling status would have a paid order that we never
            # confirm. People are trusting us with money — webhook MUST
            # confirm independently of the user's browsing behaviour.
            reg = (
                db.query(models.EventRegistration)
                .filter(
                    models.EventRegistration.payment_gateway == "phonepe",
                    (models.EventRegistration.payment_order_id == order_id)
                    | (models.EventRegistration.payment_reference == order_id),
                )
                .first()
            )
            if not reg:
                logger.info("phonepe callback for unknown order=%s", order_id)
                return {"status": "ok"}

            if reg.payment_status == "paid":
                return {"status": "ok"}  # Idempotent re-notification

            # Amount-tamper defence: must match the snapshotted fee on the row
            # (not consultation_fee — that's appointment-only).
            expected_paise = int(reg.fee_amount or 0) * 100
            if expected_paise > 0 and actual_paise > 0 and actual_paise < expected_paise:
                logger.error(
                    "phonepe callback event-reg amount mismatch reg=%s expected=%s actual=%s",
                    reg.id, expected_paise, actual_paise,
                )
                return {"status": "amount_mismatch"}

            reg.payment_status = "paid"
            reg.payment_id = order_id      # PhonePe doesn't expose a separate payment_id
            reg.payment_reference = order_id
            reg.status = "confirmed"
            db.commit()
            db.refresh(reg)

            # Receipt + confirmation email — same best-effort pattern as the
            # synchronous status-poll path.
            from utils.event_fields import parse_config
            from routers.event_registrations import (
                _generate_event_receipt,
                _send_confirmation_email,
            )
            event = db.query(models.Event).filter(models.Event.id == reg.event_id).first()
            if event:
                config = parse_config(event.registration_config)
                _generate_event_receipt(event, reg, db)
                _send_confirmation_email(event, reg, config)
                db.commit()
            logger.info("phonepe webhook confirmed event reg=%s order=%s", reg.id, order_id)
            return {"status": "ok"}

        if appt.payment_status == "paid":
            return {"status": "ok"}  # Idempotent re-notification

        # Amount-tamper defence: PhonePe tells us how much was actually paid.
        # Refuse to mark as paid if it's less than the configured fee.
        try:
            expected_paise = int(get_setting(db, "consultation_fee") or 0) * 100
        except (TypeError, ValueError):
            expected_paise = 0
        if expected_paise > 0 and actual_paise > 0 and actual_paise < expected_paise:
            logger.error(
                "phonepe callback amount mismatch order=%s expected_paise=%s actual_paise=%s",
                order_id, expected_paise, actual_paise,
            )
            # Return 200 so PhonePe doesn't retry — admin needs to investigate
            # manually, not have the same bad record come back forever.
            return {"status": "amount_mismatch"}

        _set_appt_payment_id(appt, order_id)
        _mark_paid_and_generate_receipt(appt, db)
    except Exception:
        logger.exception("phonepe callback handler crashed order=%s", order_id)
        raise HTTPException(status_code=500, detail="Internal error")

    return {"status": "ok"}


# ── Razorpay (consultation appointments) ──────────────────────────────────
#
# Mirrors the events flow but targets the Appointment row. Selected via the
# `consultation_payment_gateway` site setting. Frontend opens the Razorpay
# popup with the data returned from /razorpay/initiate-appointment, then
# POSTs the resulting (order_id, payment_id, signature) to /razorpay/verify-
# appointment which performs the same security checks as the events path
# (order-id binding, payment-id replay defence, amount cross-check).

@router.post("/razorpay/initiate-appointment")
def razorpay_initiate_appointment(
    data: RazorpayInitAppointmentRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Razorpay order for the user's pending appointment. Returns
    the data the frontend needs to open the inline checkout popup."""
    appt = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.id == data.appointment_id,
            models.Appointment.user_id == user.id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.payment_status == "paid":
        raise HTTPException(status_code=400, detail="Payment already completed")
    if appt.status in ("cancelled", "completed"):
        raise HTTPException(
            status_code=400,
            detail="This appointment has been cancelled and can no longer be paid for.",
        )

    # Server-side fee resolution — see InitiatePaymentRequest comment.
    try:
        fee_rupees = int(get_setting(db, "consultation_fee") or 0)
    except (TypeError, ValueError):
        fee_rupees = 0
    if fee_rupees <= 0:
        raise HTTPException(
            status_code=400,
            detail="Consultation fee is not configured. Contact the administrator.",
        )

    receipt = f"SPBSP_APPT_{appt.id}"
    try:
        order = razorpay_gw.create_order(
            db,
            amount_rupees=fee_rupees,
            receipt=receipt,
            notes={"appointment_id": str(appt.id)},
        )
    except razorpay_gw.RazorpayError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Stamp the order_id onto the appointment so the verify endpoint can
    # bind a verify request to this specific appointment (#1 fix from the
    # payment audit) and the webhook path can match incoming events to the
    # right row.
    _set_appt_order_id(appt, order["order_id"])
    appt.payment_id = None  # fresh init clears any stale post-success ID
    appt.status = models.AppointmentStatus.payment_pending
    db.commit()

    return {
        "order_id": order["order_id"],
        "key_id": order["key_id"],
        "amount": order["amount"],          # paise
        "currency": order["currency"],
        "receipt": order["receipt"],
        "appointment_id": appt.id,
    }


@router.post("/razorpay/verify-appointment")
def razorpay_verify_appointment(
    data: RazorpayVerifyAppointmentRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify the Razorpay popup signature for a consultation appointment
    and mark it paid. Performs:
      - ownership check (this user owns the appointment)
      - order_id binding (request order_id must match the one stored at init)
      - payment_id replay check (no other paid row uses the same payment_id)
      - HMAC signature verify against key_secret
    """
    appt = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.id == data.appointment_id,
            models.Appointment.user_id == user.id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.payment_status == "paid":
        return {"success": True, "appointment_id": appt.id}

    if not appt.payment_order_id or data.razorpay_order_id != appt.payment_order_id:
        logger.warning(
            "razorpay verify-appointment order_id mismatch appt=%s expected=%s got=%s",
            appt.id, appt.payment_order_id, data.razorpay_order_id,
        )
        raise HTTPException(status_code=400, detail="Order ID does not match this appointment.")

    duplicate = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.id != appt.id,
            models.Appointment.payment_status == "paid",
            models.Appointment.payment_id == data.razorpay_payment_id,
        )
        .first()
    )
    if duplicate:
        logger.warning(
            "razorpay verify-appointment payment_id replay appt=%s payment_id=%s used_by=%s",
            appt.id, data.razorpay_payment_id, duplicate.id,
        )
        raise HTTPException(status_code=400, detail="This payment has already been used.")

    try:
        ok = razorpay_gw.verify_payment_signature(
            db,
            razorpay_order_id=data.razorpay_order_id,
            razorpay_payment_id=data.razorpay_payment_id,
            razorpay_signature=data.razorpay_signature,
        )
    except razorpay_gw.RazorpayError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        logger.warning("razorpay verify-appointment signature mismatch appt=%s", appt.id)
        raise HTTPException(status_code=400, detail="Payment signature could not be verified.")

    _set_appt_payment_id(appt, data.razorpay_payment_id)
    _mark_paid_and_generate_receipt(appt, db)
    return {"success": True, "appointment_id": appt.id}


# ── Razorpay webhook ────────────────────────────────────────────────────────
#
# Razorpay's webhook gives us real-time confirmation independent of the
# checkout-popup signature flow. Two reasons we need it on top of the
# /razorpay-verify endpoint:
#
#   1. Reliability — if the user closes the popup before the verify call
#      lands but Razorpay still captured the payment, the webhook is the
#      only path that confirms the registration server-side.
#   2. Defence in depth — even if a buggy frontend never calls /verify, the
#      webhook eventually marks the right rows paid, with the same amount
#      and signature checks as the synchronous path.
#
# Configure in Razorpay Dashboard → Webhooks:
#   URL:    https://<your-backend>/payments/razorpay/webhook
#   Events: payment.captured, order.paid
#   Secret: a random string (saved as `payment.razorpay.webhook_secret`
#           via the admin Payment Gateways tab).

@router.post("/razorpay/webhook")
async def razorpay_webhook(
    request: Request,
    db: Session = Depends(get_db),
):
    """Razorpay webhook handler. Verifies the X-Razorpay-Signature HMAC over
    the raw request body, then on `payment.captured` / `order.paid` events
    marks the matching row (event registration first, then appointment) as
    paid — provided the captured amount matches what we expected."""
    raw_body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    try:
        ok = razorpay_gw.verify_webhook_signature(
            db, raw_body=raw_body, signature_header=signature
        )
    except razorpay_gw.RazorpayError as e:
        logger.warning("razorpay webhook config error: %s", e)
        raise HTTPException(status_code=503, detail="Webhook not configured")
    if not ok:
        logger.warning("razorpay webhook signature mismatch")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("razorpay webhook body not valid JSON")
        # 200 — Razorpay shouldn't keep retrying a malformed payload.
        return {"status": "ok"}

    event_name = payload.get("event") or ""
    if event_name not in ("payment.captured", "order.paid"):
        # Acknowledge other events (refunds, payment.failed, etc.) without
        # acting. Wire dedicated handlers later if/when refunds are added.
        return {"status": "ok"}

    payment_entity = (
        payload.get("payload", {}).get("payment", {}).get("entity") or {}
    )
    order_id = payment_entity.get("order_id") or ""
    payment_id = payment_entity.get("id") or ""
    amount_paise = int(payment_entity.get("amount") or 0)
    if not order_id or not payment_id:
        logger.warning("razorpay webhook missing order_id/payment_id event=%s", event_name)
        return {"status": "ok"}

    try:
        # Look up an event registration by the gateway order_id (now its own
        # column — no longer overwritten by payment_id at verify time). If
        # the row already has payment_id set, the synchronous /verify path
        # already confirmed it; the webhook lookup still finds the row by
        # order_id and is idempotent below.
        reg = (
            db.query(models.EventRegistration)
            .filter(
                models.EventRegistration.payment_gateway == "razorpay",
                (models.EventRegistration.payment_order_id == order_id)
                | (models.EventRegistration.payment_reference == order_id),
            )
            .first()
        )
        if reg:
            if reg.payment_status == "paid":
                return {"status": "ok"}  # idempotent

            expected_paise = int(reg.fee_amount or 0) * 100
            if expected_paise > 0 and amount_paise > 0 and amount_paise < expected_paise:
                logger.error(
                    "razorpay webhook amount mismatch reg=%s expected=%s actual=%s",
                    reg.id, expected_paise, amount_paise,
                )
                return {"status": "amount_mismatch"}

            # Replay across registrations: the same payment_id can't belong
            # to two paid rows.
            duplicate = (
                db.query(models.EventRegistration)
                .filter(
                    models.EventRegistration.id != reg.id,
                    models.EventRegistration.payment_gateway == "razorpay",
                    models.EventRegistration.payment_status == "paid",
                    models.EventRegistration.payment_id == payment_id,
                )
                .first()
            )
            if duplicate:
                logger.warning(
                    "razorpay webhook payment_id already used reg=%s payment_id=%s used_by=%s",
                    reg.id, payment_id, duplicate.id,
                )
                return {"status": "duplicate_payment"}

            reg.payment_status = "paid"
            reg.payment_id = payment_id
            reg.payment_reference = payment_id
            reg.status = "confirmed"
            db.commit()
            db.refresh(reg)

            # Receipt + email — same as the synchronous /verify path. Both
            # are best-effort; failures are logged but never roll back the
            # confirmed payment state.
            from utils.event_fields import parse_config
            from routers.event_registrations import (
                _generate_event_receipt,
                _send_confirmation_email,
            )
            event = db.query(models.Event).filter(models.Event.id == reg.event_id).first()
            if event:
                config = parse_config(event.registration_config)
                _generate_event_receipt(event, reg, db)
                _send_confirmation_email(event, reg, config)
                db.commit()
            logger.info("razorpay webhook confirmed event reg=%s payment=%s", reg.id, payment_id)
            return {"status": "ok"}

        # No event registration matched. Try appointments next — useful once
        # consultation appointments switch to Razorpay (today they're
        # PhonePe-only but we want the webhook ready).
        appt = (
            db.query(models.Appointment)
            .filter(
                (models.Appointment.payment_order_id == order_id)
                | (models.Appointment.payment_reference == order_id)
            )
            .first()
        )
        if appt and appt.payment_status != "paid":
            try:
                expected_paise = int(get_setting(db, "consultation_fee") or 0) * 100
            except (TypeError, ValueError):
                expected_paise = 0
            if expected_paise > 0 and amount_paise > 0 and amount_paise < expected_paise:
                logger.error(
                    "razorpay webhook appt amount mismatch order=%s expected=%s actual=%s",
                    order_id, expected_paise, amount_paise,
                )
                return {"status": "amount_mismatch"}
            _set_appt_payment_id(appt, payment_id)
            _mark_paid_and_generate_receipt(appt, db)
            logger.info("razorpay webhook confirmed appt=%s payment=%s", appt.id, payment_id)

    except Exception:
        logger.exception("razorpay webhook handler crashed order=%s", order_id)
        raise HTTPException(status_code=500, detail="Internal error")

    return {"status": "ok"}
