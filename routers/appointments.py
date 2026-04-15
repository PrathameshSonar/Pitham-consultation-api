import os
import io
import json
import shutil
import re
import zipfile
import aiofiles
import bleach
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import List, Optional
import uuid

from database import get_db
import models
import schemas
from utils.auth import get_current_user, require_admin, require_super_admin
from utils.audit import log_action
from utils.email import send_appointment_confirmation, send_reschedule_notification, send_completion_notification
from utils.zoom import create_meeting as zoom_create_meeting, ZoomError

router = APIRouter(tags=["appointments"])

UPLOAD_DIR = "uploads/selfies"
ANALYSIS_DIR = "uploads/analysis"
ASSIGNED_DIR = "uploads/documents"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(ANALYSIS_DIR, exist_ok=True)
os.makedirs(ASSIGNED_DIR, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _check_slot_conflict(
    db: Session, appt_id: int, date: str, time: str
):
    """Raise 400 if another appointment is already booked at that date+time."""
    conflict = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.id != appt_id,
            models.Appointment.scheduled_date == date,
            models.Appointment.scheduled_time == time,
            models.Appointment.status.in_([
                models.AppointmentStatus.scheduled,
                models.AppointmentStatus.rescheduled,
            ]),
        )
        .first()
    )
    if conflict:
        raise HTTPException(
            status_code=400,
            detail="This slot is already booked. Please pick a different time.",
        )


# ── User: book appointment ─────────────────────────────────────────────────────

def _check_booking_allowed(db: Session):
    """Check admin settings: booking enabled, limit, deadline."""
    enabled = db.query(models.SiteSetting).filter(models.SiteSetting.key == "booking_enabled").first()
    if enabled and enabled.value == "false":
        msg_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "booking_hold_message").first()
        detail = msg_row.value if msg_row and msg_row.value else "Consultation booking is currently on hold."
        raise HTTPException(status_code=403, detail=detail)

    deadline_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "booking_limit_deadline").first()
    if deadline_row and deadline_row.value:
        try:
            dl = datetime.fromisoformat(deadline_row.value.replace("Z", "+00:00"))
            # Strip timezone info to compare naive-to-naive
            dl_naive = dl.replace(tzinfo=None)
            if datetime.utcnow() > dl_naive:
                raise HTTPException(status_code=403, detail="Booking deadline has passed.")
        except ValueError:
            pass

    limit_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "booking_limit").first()
    if limit_row and limit_row.value:
        max_limit = int(limit_row.value)
        if max_limit > 0:
            # Only count appointments that are not yet completed or cancelled
            active = db.query(models.Appointment).filter(
                models.Appointment.status.in_([
                    models.AppointmentStatus.pending,
                    models.AppointmentStatus.payment_pending,
                    models.AppointmentStatus.payment_verified,
                    models.AppointmentStatus.scheduled,
                    models.AppointmentStatus.rescheduled,
                ])
            ).count()
            if active >= max_limit:
                raise HTTPException(status_code=403, detail="Booking limit reached. Please try again later.")


def _clean(val: str) -> str:
    return bleach.clean(val.strip(), tags=[], strip=True) if val else ""


@router.post("/appointments", response_model=schemas.AppointmentOut)
async def book_appointment(
    name: str = Form(...),
    email: str = Form(...),
    mobile: str = Form(...),
    dob: str = Form(...),
    tob: str = Form(...),
    birth_place: str = Form(...),
    problem: str = Form(...),
    selfie: Optional[UploadFile] = File(None),
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _check_booking_allowed(db)

    # Sanitize all inputs
    name = _clean(name)
    email = _clean(email)
    mobile = re.sub(r"[^\d+\-\s]", "", mobile.strip())
    dob = _clean(dob)
    tob = _clean(tob)
    birth_place = _clean(birth_place)
    problem = _clean(problem)

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name is required")
    if len(problem) < 5:
        raise HTTPException(status_code=400, detail="Please describe your problem")

    selfie_path = None
    if selfie and selfie.filename:
        # Validate file type
        allowed_types = {"image/jpeg", "image/png", "image/webp", "image/gif"}
        if selfie.content_type and selfie.content_type not in allowed_types:
            raise HTTPException(status_code=400, detail="Only image files are allowed for selfie.")
        content = await selfie.read()
        # Validate file size (max 5MB)
        if len(content) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Selfie file size must be under 5MB.")
        # Sanitize filename
        safe_name = re.sub(r"[^\w.\-]", "_", selfie.filename)
        filename = f"{user.id}_{safe_name}"
        selfie_path = os.path.join(UPLOAD_DIR, filename)
        async with aiofiles.open(selfie_path, "wb") as f:
            await f.write(content)

    # Snapshot the current T&C so the user's agreed version is preserved
    terms_row = db.query(models.SiteSetting).filter(
        models.SiteSetting.key == "consultation_terms"
    ).first()
    agreed_terms = terms_row.value if terms_row else ""

    appt = models.Appointment(
        user_id=user.id,
        name=name,
        email=email,
        mobile=mobile,
        dob=dob,
        tob=tob,
        birth_place=birth_place,
        problem=problem,
        selfie_path=selfie_path,
        agreed_terms=agreed_terms,
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return appt


# ── User: my appointments ──────────────────────────────────────────────────────

@router.get("/appointments/my", response_model=List[schemas.AppointmentOut])
def my_appointments(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Appointment)
        .filter(models.Appointment.user_id == user.id)
        .order_by(models.Appointment.created_at.desc())
        .all()
    )


# ── User: download .ics calendar file ─────────────────────────────────────────

@router.get("/appointments/{appt_id}/calendar")
def user_download_ics(
    appt_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from fastapi.responses import StreamingResponse
    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.id == appt_id, models.Appointment.user_id == user.id)
        .first()
    )
    if not appt or not appt.scheduled_date or not appt.scheduled_time:
        raise HTTPException(status_code=404, detail="No scheduled date/time found")

    dt_start = f"{appt.scheduled_date.replace('-', '')}T{appt.scheduled_time.replace(':', '')}00"
    ics = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Pitham//Consultation//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{dt_start}\r\nDURATION:PT45M\r\n"
        f"SUMMARY:Pitham Consultation\r\n"
        f"DESCRIPTION:{appt.problem[:200]}\r\n"
        f"LOCATION:{appt.zoom_link or 'Zoom'}\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    return StreamingResponse(
        iter([ics]),
        media_type="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=consultation_{appt_id}.ics"},
    )


# ── User: cancel own unpaid appointment ──────────────────────────────────────

@router.delete("/appointments/{appt_id}")
def cancel_appointment(
    appt_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.id == appt_id, models.Appointment.user_id == user.id)
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.payment_status == "paid":
        raise HTTPException(status_code=400, detail="Cannot cancel a paid appointment. Please contact admin.")
    if appt.status == "completed":
        raise HTTPException(status_code=400, detail="Cannot cancel a completed appointment.")

    db.delete(appt)
    db.commit()
    return {"message": "Appointment cancelled and removed."}


# ── User: generate receipt for own appointment ────────────────────────────────

@router.post("/appointments/{appt_id}/generate-receipt")
def user_generate_receipt(
    appt_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from utils.pdf_receipt import generate_receipt as gen_receipt

    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.id == appt_id, models.Appointment.user_id == user.id)
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.payment_status != "paid":
        raise HTTPException(status_code=400, detail="Payment not verified yet")

    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = fee_row.value if fee_row else "500"

    # Use the T&C the user agreed to at booking time
    terms_html = appt.agreed_terms or ""

    receipt_path = gen_receipt(
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
    db.commit()
    return {"message": "Receipt generated", "receipt_path": receipt_path}


# ── User: generate invoice for own appointment ────────────────────────────────

@router.post("/appointments/{appt_id}/generate-invoice")
def user_generate_invoice(
    appt_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from utils.pdf_invoice import generate_invoice

    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.id == appt_id, models.Appointment.user_id == user.id)
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.payment_status != "paid":
        raise HTTPException(status_code=400, detail="Payment not verified yet")

    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = fee_row.value if fee_row else "500"

    invoice_path = generate_invoice(
        appointment_id=appt.id,
        name=appt.name,
        email=appt.email,
        mobile=appt.mobile,
        payment_reference=appt.payment_reference or "",
        fee=fee,
        booked_on=appt.created_at.strftime("%d %B %Y") if appt.created_at else "",
    )
    return {"message": "Invoice generated", "invoice_path": invoice_path}


# ── Admin: all appointments ────────────────────────────────────────────────────

@router.get("/admin/appointments", response_model=List[schemas.AppointmentOut])
def all_appointments(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.query(models.Appointment).order_by(models.Appointment.created_at.desc()).all()


# ── Admin: verify payment ──────────────────────────────────────────────────────

@router.put("/admin/appointments/{appt_id}/verify-payment")
def verify_payment(
    appt_id: int,
    data: schemas.VerifyPaymentRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    appt.payment_status = "paid"
    appt.payment_reference = data.payment_reference
    appt.status = models.AppointmentStatus.payment_verified
    db.commit()
    return {"message": "Payment verified"}


# ── Admin: assign time slot + zoom link ───────────────────────────────────────

@router.put("/admin/appointments/{appt_id}/assign-slot")
def assign_slot(
    appt_id: int,
    data: schemas.AssignSlotRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    _check_slot_conflict(db, appt_id, data.scheduled_date, data.scheduled_time)

    appt.scheduled_date = data.scheduled_date
    appt.scheduled_time = data.scheduled_time
    appt.zoom_link = data.zoom_link
    appt.notes = data.notes
    appt.status = models.AppointmentStatus.scheduled
    db.commit()

    log_action(db, admin.id, "assign_slot", "appointment", appt.id,
               f"Date: {data.scheduled_date}, Time: {data.scheduled_time}")

    user = db.query(models.User).filter(models.User.id == appt.user_id).first()
    if user:
        send_appointment_confirmation(
            to=user.email,
            name=user.name,
            scheduled_date=data.scheduled_date,
            scheduled_time=data.scheduled_time,
            zoom_link=data.zoom_link,
        )
    return {"message": "Slot assigned and email sent"}


# ── Admin: reschedule ──────────────────────────────────────────────────────────

@router.put("/admin/appointments/{appt_id}/reschedule")
def reschedule(
    appt_id: int,
    data: schemas.RescheduleRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    _check_slot_conflict(db, appt_id, data.scheduled_date, data.scheduled_time)

    appt.scheduled_date = data.scheduled_date
    appt.scheduled_time = data.scheduled_time
    if data.zoom_link:
        appt.zoom_link = data.zoom_link
    appt.status = models.AppointmentStatus.rescheduled
    db.commit()

    log_action(db, admin.id, "reschedule", "appointment", appt.id,
               f"New date: {data.scheduled_date}, Time: {data.scheduled_time}")

    user = db.query(models.User).filter(models.User.id == appt.user_id).first()
    if user:
        send_reschedule_notification(
            to=user.email,
            name=user.name,
            scheduled_date=data.scheduled_date,
            scheduled_time=data.scheduled_time,
            zoom_link=appt.zoom_link or "",
            reason=data.reason or "",
        )
    return {"message": "Appointment rescheduled and email sent"}


# ── Admin: mark as completed (optionally with analysis file) ──────────────────

@router.post("/admin/appointments/{appt_id}/complete")
async def mark_completed(
    appt_id: int,
    analysis_notes: str = Form(""),
    recording_link: str = Form(""),
    gallery_doc_ids: str = Form(""),          # JSON array: "[1,2,3]"
    analysis_file: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    if analysis_file and analysis_file.filename:
        content = await analysis_file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="Analysis file must be under 10MB.")
        safe_name = re.sub(r"[^\w.\-]", "_", analysis_file.filename)
        filename = f"{appt.id}_{safe_name}"
        path = os.path.join(ANALYSIS_DIR, filename)
        async with aiofiles.open(path, "wb") as f:
            await f.write(content)
        appt.analysis_path = path

    if analysis_notes:
        appt.analysis_notes = analysis_notes

    if recording_link:
        appt.recording_link = recording_link

    # Assign sadhna documents from gallery to the user
    if gallery_doc_ids:
        try:
            doc_ids = json.loads(gallery_doc_ids)
            if isinstance(doc_ids, list) and doc_ids:
                batch_id = str(uuid.uuid4())
                for did in doc_ids:
                    template = (
                        db.query(models.Document)
                        .filter(models.Document.id == did, models.Document.user_id.is_(None))
                        .first()
                    )
                    if not template:
                        continue
                    orig_name = os.path.basename(template.file_path)
                    new_name = f"{appt.user_id}_{template.id}_{orig_name}"
                    new_path = os.path.join(ASSIGNED_DIR, new_name)
                    try:
                        shutil.copyfile(template.file_path, new_path)
                    except OSError:
                        continue
                    doc = models.Document(
                        user_id=appt.user_id,
                        title=template.title,
                        description=template.description,
                        file_path=new_path,
                        uploaded_by=admin.id,
                        source_template_id=template.id,
                        batch_id=batch_id,
                        batch_label=f"Consultation #{appt.id}",
                    )
                    db.add(doc)
        except (json.JSONDecodeError, TypeError):
            pass

    appt.status = models.AppointmentStatus.completed
    db.commit()

    log_action(db, admin.id, "mark_completed", "appointment", appt.id, f"For: {appt.name}")

    # Send completion email
    try:
        send_completion_notification(
            to=appt.email,
            name=appt.name,
            booking_id=appt.id,
            analysis_path=appt.analysis_path or "",
            recording_link=recording_link or appt.recording_link or "",
        )
    except Exception:
        pass

    return {"message": "Appointment marked as completed"}


# ── Admin: cancel any appointment (no refund) ─────────────────────────────────

@router.delete("/admin/appointments/{appt_id}")
def admin_cancel_appointment(
    appt_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")
    if appt.status == "completed":
        raise HTTPException(status_code=400, detail="Cannot cancel a completed appointment.")

    name = appt.name
    appt.status = models.AppointmentStatus.cancelled
    db.commit()

    log_action(db, admin.id, "cancel_appointment", "appointment", appt.id,
               f"Cancelled: {name} (no refund)")

    # Notify user
    try:
        from utils.email import send_email
        send_email(
            to=appt.email,
            subject="Consultation Cancelled — Pitham",
            html_body=f"""
            <div style="font-family:'Poppins',sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
              <h3>Namaste {appt.name},</h3>
              <p>Your consultation <strong>PITHAM-{appt.id}</strong> has been cancelled by the admin.</p>
              <p style="color:#C62828"><strong>Note:</strong> As per our no-refund policy, the consultation fee is non-refundable.</p>
              <p>If you have questions, please raise a query on your dashboard.</p>
              <p>Regards,<br><strong>Pitham Consultation</strong></p>
            </div>
            """,
        )
    except Exception:
        pass

    return {"message": "Appointment cancelled. No refund as per policy."}


# ── Admin: generate/regenerate receipt PDF ────────────────────────────────────

@router.post("/admin/appointments/{appt_id}/generate-receipt")
def admin_generate_receipt(
    appt_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from utils.pdf_receipt import generate_receipt

    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = fee_row.value if fee_row else "500"

    # Use the T&C the user agreed to at booking time
    terms_html = appt.agreed_terms or ""

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
    db.commit()
    return {"message": "Receipt generated", "receipt_path": receipt_path}


# ── Admin: generate CA invoice / bill receipt ─────────────────────────────────

@router.post("/admin/appointments/{appt_id}/generate-invoice")
def admin_generate_invoice(
    appt_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from utils.pdf_invoice import generate_invoice

    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Appointment not found")

    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = fee_row.value if fee_row else "500"

    invoice_path = generate_invoice(
        appointment_id=appt.id,
        name=appt.name,
        email=appt.email,
        mobile=appt.mobile,
        payment_reference=appt.payment_reference or "",
        fee=fee,
        booked_on=appt.created_at.strftime("%d %B %Y") if appt.created_at else "",
    )
    return {"message": "Invoice generated", "invoice_path": invoice_path}


# ── Admin: bulk download invoices as zip (date filter) ────────────────────────

@router.get("/admin/invoices/download")
def admin_download_invoices(
    date_from: str = Query(..., description="Start date YYYY-MM-DD"),
    date_to: str = Query(..., description="End date YYYY-MM-DD"),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    from utils.pdf_invoice import generate_invoice

    try:
        d_from = datetime.strptime(date_from, "%Y-%m-%d")
        d_to = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    appts = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.payment_status == "paid",
            models.Appointment.created_at >= d_from,
            models.Appointment.created_at <= d_to,
        )
        .order_by(models.Appointment.created_at)
        .all()
    )

    if not appts:
        raise HTTPException(status_code=404, detail="No paid appointments found in this date range.")

    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = fee_row.value if fee_row else "500"

    # Generate all invoices and zip them
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for appt in appts:
            invoice_path = generate_invoice(
                appointment_id=appt.id,
                name=appt.name,
                email=appt.email,
                mobile=appt.mobile,
                payment_reference=appt.payment_reference or "",
                fee=fee,
                booked_on=appt.created_at.strftime("%d %B %Y") if appt.created_at else "",
            )
            filename = f"invoice_{appt.id}_{appt.name.replace(' ', '_')}.pdf"
            zf.write(invoice_path, filename)

    zip_buffer.seek(0)
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename=invoices_{date_from}_to_{date_to}.zip"
        },
    )


# ── Admin: create Zoom meeting ─────────────────────────────────────────────────

@router.post("/admin/zoom/create-meeting", response_model=schemas.ZoomMeetingResponse)
def create_zoom_meeting(
    data: schemas.CreateZoomMeetingRequest,
    admin: models.User = Depends(require_admin),
):
    try:
        meeting = zoom_create_meeting(
            topic=data.topic,
            start_date=data.scheduled_date,
            start_time=data.scheduled_time,
            duration=data.duration,
        )
        return meeting
    except ZoomError as e:
        raise HTTPException(status_code=500, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Unexpected error: {e}")
