"""
Admin tools: audit log, global search, CSV export, appointment reminders.
"""

import csv
import io
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import or_
from typing import Optional

from database import get_db
import models
from utils.auth import require_admin, require_super_admin
from utils.audit import log_action
from utils.email import send_email
from utils.site_settings import get_setting

router = APIRouter(prefix="/admin", tags=["admin-tools"])


# ── Audit Log ─────────────────────────────────────────────────────────────────

@router.get("/audit-log")
def get_audit_log(
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
    action: Optional[str] = Query(None),
    admin_id: Optional[int] = Query(None, description="Filter by admin user ID"),
    entity_type: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    sort: Optional[str] = Query("newest", description="newest or oldest"),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.AuditLog)

    if action:
        q = q.filter(models.AuditLog.action == action)
    if admin_id:
        q = q.filter(models.AuditLog.admin_id == admin_id)
    if entity_type:
        q = q.filter(models.AuditLog.entity_type == entity_type)
    if date_from:
        q = q.filter(models.AuditLog.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(models.AuditLog.created_at <= datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59))

    if sort == "oldest":
        q = q.order_by(models.AuditLog.created_at.asc())
    else:
        q = q.order_by(models.AuditLog.created_at.desc())

    total = q.count()
    logs = q.offset((page - 1) * limit).limit(limit).all()

    # Fetch admin names in one query
    admin_ids_set = {l.admin_id for l in logs}
    admins = {u.id: u.name for u in db.query(models.User).filter(models.User.id.in_(admin_ids_set)).all()} if admin_ids_set else {}

    # Get distinct actions and admin names for filter dropdowns
    all_actions = [r[0] for r in db.query(models.AuditLog.action).distinct().all()]
    all_admin_ids = [r[0] for r in db.query(models.AuditLog.admin_id).distinct().all()]
    all_admins = {u.id: u.name for u in db.query(models.User).filter(models.User.id.in_(all_admin_ids)).all()} if all_admin_ids else {}

    return {
        "total": total,
        "page": page,
        "filters": {
            "actions": sorted(all_actions),
            "admins": [{"id": k, "name": v} for k, v in sorted(all_admins.items(), key=lambda x: x[1])],
        },
        "logs": [
            {
                "id": l.id,
                "admin_id": l.admin_id,
                "admin_name": admins.get(l.admin_id, f"Admin #{l.admin_id}"),
                "action": l.action,
                "entity_type": l.entity_type,
                "entity_id": l.entity_id,
                "details": l.details,
                "created_at": l.created_at.isoformat() if l.created_at else None,
            }
            for l in logs
        ],
    }


# ── Global Search ─────────────────────────────────────────────────────────────

@router.get("/search")
def global_search(
    q: str = Query(..., min_length=2),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    term = f"%{q.strip()}%"

    users = (
        db.query(models.User)
        .filter(
            models.User.role == "user",
            or_(
                models.User.name.ilike(term),
                models.User.email.ilike(term),
                models.User.mobile.ilike(term),
                models.User.city.ilike(term),
            ),
        )
        .limit(20)
        .all()
    )

    appointments = (
        db.query(models.Appointment)
        .filter(
            or_(
                models.Appointment.name.ilike(term),
                models.Appointment.email.ilike(term),
                models.Appointment.mobile.ilike(term),
                models.Appointment.problem.ilike(term),
            ),
        )
        .limit(20)
        .all()
    )

    documents = (
        db.query(models.Document)
        .filter(models.Document.title.ilike(term))
        .limit(20)
        .all()
    )

    return {
        "users": [{"id": u.id, "name": u.name, "email": u.email, "mobile": u.mobile} for u in users],
        "appointments": [
            {"id": a.id, "name": a.name, "email": a.email, "status": a.status, "created_at": a.created_at.isoformat() if a.created_at else None}
            for a in appointments
        ],
        "documents": [{"id": d.id, "title": d.title, "user_id": d.user_id} for d in documents],
    }


# ── CSV Export ────────────────────────────────────────────────────────────────

@router.get("/export/users")
def export_users_csv(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    users = db.query(models.User).filter(models.User.role == "user").order_by(models.User.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Email", "Mobile", "DOB", "TOB", "Birth Place", "City", "State", "Country", "Joined"])
    for u in users:
        writer.writerow([u.id, u.name, u.email or "", u.mobile, u.dob, u.tob, u.birth_place, u.city, u.state, u.country,
                         u.created_at.strftime("%Y-%m-%d") if u.created_at else ""])
    output.seek(0)
    log_action(db, admin.id, "export_users", "export", 0, f"{len(users)} users")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=users_{datetime.utcnow().strftime('%Y%m%d')}.csv"},
    )


@router.get("/export/appointments")
def export_appointments_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.Appointment).order_by(models.Appointment.created_at.desc())
    if date_from:
        q = q.filter(models.Appointment.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(models.Appointment.created_at <= datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59))
    appts = q.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Email", "Mobile", "DOB", "TOB", "Birth Place", "Problem", "Status",
                     "Payment", "Scheduled Date", "Scheduled Time", "Booked On"])
    for a in appts:
        writer.writerow([a.id, a.name, a.email, a.mobile, a.dob, a.tob, a.birth_place,
                         a.problem[:100], a.status, a.payment_status,
                         a.scheduled_date or "", a.scheduled_time or "",
                         a.created_at.strftime("%Y-%m-%d") if a.created_at else ""])
    output.seek(0)
    log_action(db, admin.id, "export_appointments", "export", 0, f"{len(appts)} appointments")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=appointments_{datetime.utcnow().strftime('%Y%m%d')}.csv"},
    )


@router.get("/export/payments")
def export_payments_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.Appointment).filter(models.Appointment.payment_status == "paid")
    if date_from:
        q = q.filter(models.Appointment.created_at >= datetime.strptime(date_from, "%Y-%m-%d"))
    if date_to:
        q = q.filter(models.Appointment.created_at <= datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59))
    appts = q.order_by(models.Appointment.created_at.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Booking ID", "Name", "Email", "Mobile", "Payment Ref", "Amount", "Date"])

    fee = get_setting(db, "consultation_fee")

    for a in appts:
        writer.writerow([f"SPBSP-{a.id}", a.name, a.email, a.mobile, a.payment_reference or "",
                         fee, a.created_at.strftime("%Y-%m-%d") if a.created_at else ""])
    output.seek(0)
    log_action(db, admin.id, "export_payments", "export", 0, f"{len(appts)} payments")
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=payments_{datetime.utcnow().strftime('%Y%m%d')}.csv"},
    )


# ── Send Appointment Reminders ────────────────────────────────────────────────

@router.post("/send-reminders")
def send_appointment_reminders(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Send reminders for appointments scheduled tomorrow."""
    tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
    appts = (
        db.query(models.Appointment)
        .filter(
            models.Appointment.scheduled_date == tomorrow,
            models.Appointment.status.in_(["scheduled", "rescheduled"]),
        )
        .all()
    )

    sent = 0
    for a in appts:
        if not a.email:
            continue
        body = f"""
        <div style="font-family:'Poppins',Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
          <div style="text-align:center;margin-bottom:20px">
            <h2 style="color:#7B1E1E;margin:4px 0">Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar</h2>
          </div>
          <h3>Namaste {a.name},</h3>
          <p>This is a reminder that your consultation with Guruji is <strong>tomorrow</strong>.</p>
          <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
            <table style="border-collapse:collapse">
              <tr><td style="padding:4px 16px 4px 0"><strong>Date:</strong></td><td>{a.scheduled_date}</td></tr>
              <tr><td style="padding:4px 16px 4px 0"><strong>Time:</strong></td><td>{a.scheduled_time}</td></tr>
              {f'<tr><td style="padding:4px 16px 4px 0"><strong>Zoom:</strong></td><td><a href="{a.zoom_link}" style="color:#E65100">Join Meeting</a></td></tr>' if a.zoom_link else ''}
            </table>
          </div>
          <p>Please join the Zoom meeting <strong>5 minutes before</strong> the scheduled time.</p>
          <p style="margin-top:24px">Regards,<br><strong>Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar</strong></p>
        </div>
        """
        send_email(a.email, "Consultation Reminder — Tomorrow", body)
        sent += 1

    log_action(db, admin.id, "send_reminders", "appointment", 0, f"{sent} reminders for {tomorrow}")
    return {"message": f"Reminders sent for {sent} appointment(s) scheduled on {tomorrow}."}


# ── Calendar .ics download ────────────────────────────────────────────────────

@router.get("/appointments/{appt_id}/calendar")
def download_ics(
    appt_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    appt = db.query(models.Appointment).filter(models.Appointment.id == appt_id).first()
    if not appt or not appt.scheduled_date or not appt.scheduled_time:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="No scheduled date/time found")

    # Build .ics content
    dt_start = f"{appt.scheduled_date.replace('-', '')}T{appt.scheduled_time.replace(':', '')}00"
    ics = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//SPBSP//Consultation//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"DTSTART:{dt_start}\r\n"
        f"DURATION:PT45M\r\n"
        f"SUMMARY:SPBSP, Ahilyanagar — {appt.name}\r\n"
        f"DESCRIPTION:{appt.problem[:200]}\r\n"
        f"LOCATION:{appt.zoom_link or 'Zoom'}\r\n"
        f"URL:{appt.zoom_link or ''}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    return StreamingResponse(
        iter([ics]),
        media_type="text/calendar",
        headers={"Content-Disposition": f"attachment; filename=consultation_{appt_id}.ics"},
    )
