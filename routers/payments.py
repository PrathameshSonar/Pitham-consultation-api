from fastapi import APIRouter, Depends, HTTPException, Request
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

router = APIRouter(prefix="/payments", tags=["payments"])


class InitiatePaymentRequest(BaseModel):
    appointment_id: int
    amount: float  # in rupees


class InitiatePaymentResponse(BaseModel):
    redirect_url: str
    transaction_id: str


class PaymentStatusResponse(BaseModel):
    success: bool
    state: str
    transaction_id: str


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

    try:
        result = initiate_payment(
            appointment_id=appt.id,
            amount_rupees=data.amount,
            user_mobile=user.mobile,
        )
    except PhonePeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    appt.payment_reference = result["merchant_order_id"]
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
            models.Appointment.payment_reference == transaction_id,
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
    try:
        authorization = request.headers.get("Authorization", "")
        body = await request.body()
        body_str = body.decode("utf-8")

        result = validate_callback(authorization, body_str)

        if result["event"] == "checkout.order.completed" and result["state"] == "COMPLETED":
            order_id = result["merchant_order_id"]
            if order_id:
                appt = (
                    db.query(models.Appointment)
                    .filter(models.Appointment.payment_reference == order_id)
                    .first()
                )
                if appt and appt.payment_status != "paid":
                    _mark_paid_and_generate_receipt(appt, db)
    except Exception:
        pass

    return {"status": "ok"}
