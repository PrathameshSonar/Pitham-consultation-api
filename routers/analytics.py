from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from datetime import datetime, timedelta
from typing import Optional


from database import get_db
import models
from utils.auth import require_admin
from utils.permissions import has_section_access, is_super_admin
from utils.site_settings import get_consultation_fee


router = APIRouter(prefix="/admin/analytics", tags=["analytics"])




def _month_expr(db: Session, column):
    """Cross-dialect 'YYYY-MM' string from a DateTime column.
    PostgreSQL → to_char, MySQL → date_format, SQLite → strftime."""
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        return func.to_char(column, "YYYY-MM")
    if dialect in ("mysql", "mariadb"):
        return func.date_format(column, "%Y-%m")
    # SQLite + anything else we run locally
    return func.strftime("%Y-%m", column)




def _parse_window(
    date_from: Optional[str], date_to: Optional[str], default_days: int = 30,
) -> tuple[datetime, datetime]:
    """Resolve the dashboard window. Both bounds optional; defaults to the
    last `default_days` days ending now. Raises 400 on malformed input so
    the frontend can show a clear error rather than getting silently
    different numbers."""
    now = datetime.utcnow()
    try:
        end = (
            datetime.fromisoformat(date_to).replace(hour=23, minute=59, second=59)
            if date_to else now
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
    try:
        start = (
            datetime.fromisoformat(date_from).replace(hour=0, minute=0, second=0)
            if date_from else end - timedelta(days=default_days)
        )
    except ValueError:
        raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if start > end:
        raise HTTPException(status_code=400, detail="date_from must be on or before date_to")
    return start, end




@router.get("")
def get_analytics(
    # Date window for the per-month charts AND the "recent activity"
    # block. Defaults to the last 30 days so the dashboard load doesn't
    # blast through the entire history every time.
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    window_start, window_end = _parse_window(date_from, date_to, default_days=30)


    # ── Totals ── (always lifetime; not affected by the window)
    total_users = db.query(models.User).filter(models.User.role == "user").count()
    total_appointments = db.query(models.Appointment).count()
    total_documents = db.query(models.Document).filter(models.Document.user_id.is_not(None)).count()
    total_recordings = db.query(models.Recording).count()
    total_queries = db.query(models.Query).count()


    # ── Appointment status breakdown (for pie chart) — windowed by created_at ──
    status_counts = (
        db.query(models.Appointment.status, func.count(models.Appointment.id))
        .filter(
            models.Appointment.created_at >= window_start,
            models.Appointment.created_at <= window_end,
        )
        .group_by(models.Appointment.status)
        .all()
    )
    appointment_by_status = [{"status": s, "count": c} for s, c in status_counts]


    # ── Payment breakdown (for pie chart) — windowed ──
    payment_counts = (
        db.query(models.Appointment.payment_status, func.count(models.Appointment.id))
        .filter(
            models.Appointment.created_at >= window_start,
            models.Appointment.created_at <= window_end,
        )
        .group_by(models.Appointment.payment_status)
        .all()
    )
    appointment_by_payment = [{"status": s, "count": c} for s, c in payment_counts]


    # ── Appointments per month — windowed ──
    appt_month = _month_expr(db, models.Appointment.created_at).label("month")
    monthly_raw = (
        db.query(appt_month, func.count(models.Appointment.id))
        .filter(
            models.Appointment.created_at >= window_start,
            models.Appointment.created_at <= window_end,
        )
        .group_by(appt_month)
        .order_by(appt_month)
        .all()
    )
    appointments_per_month = [{"month": m, "count": c} for m, c in monthly_raw]


    # ── New users per month — windowed ──
    user_month = _month_expr(db, models.User.created_at).label("month")
    users_monthly_raw = (
        db.query(user_month, func.count(models.User.id))
        .filter(
            models.User.created_at >= window_start,
            models.User.created_at <= window_end,
            models.User.role == "user",
        )
        .group_by(user_month)
        .order_by(user_month)
        .all()
    )
    users_per_month = [{"month": m, "count": c} for m, c in users_monthly_raw]


    # ── Recent activity — windowed ──
    new_users_30d = db.query(models.User).filter(
        models.User.created_at >= window_start,
        models.User.created_at <= window_end,
        models.User.role == "user",
    ).count()
    new_appts_30d = db.query(models.Appointment).filter(
        models.Appointment.created_at >= window_start,
        models.Appointment.created_at <= window_end,
    ).count()
    completed_30d = db.query(models.Appointment).filter(
        models.Appointment.status == "completed",
        models.Appointment.updated_at >= window_start,
        models.Appointment.updated_at <= window_end,
    ).count()
    open_queries = db.query(models.Query).filter(models.Query.status == "open").count()


    # ── Revenue & consultation time ──
    fee = get_consultation_fee(db)


    total_completed = db.query(models.Appointment).filter(
        models.Appointment.status == "completed"
    ).count()
    total_revenue = total_completed * fee
    total_hours = (total_completed * 30) / 60  # avg 30 min per consultation


    # Monthly revenue — windowed
    completed_month = _month_expr(db, models.Appointment.updated_at).label("month")
    completed_monthly_raw = (
        db.query(completed_month, func.count(models.Appointment.id))
        .filter(
            models.Appointment.status == "completed",
            models.Appointment.updated_at >= window_start,
            models.Appointment.updated_at <= window_end,
        )
        .group_by(completed_month)
        .order_by(completed_month)
        .all()
    )
    revenue_per_month = [{"month": m, "count": c, "revenue": c * fee} for m, c in completed_monthly_raw]


    # Per-section visibility — moderators only see numbers they have access to.
    # Counts are aggregate but we still hide them per-section to mirror the
    # dashboard UI and avoid leaking activity volume across sections.
    super_ = is_super_admin(admin)
    can_appts   = super_ or has_section_access(admin, "appointments")
    can_users   = super_ or has_section_access(admin, "users")
    can_docs    = super_ or has_section_access(admin, "documents")
    can_queries = super_ or has_section_access(admin, "queries")


    totals: dict = {}
    if can_users:    totals["users"] = total_users
    if can_appts:    totals["appointments"] = total_appointments
    if can_docs:     totals["documents"] = total_documents
    if can_appts:    totals["recordings"] = total_recordings  # recordings tied to appts
    if can_queries:  totals["queries"] = total_queries


    recent_30d: dict = {}
    if can_users:    recent_30d["new_users"] = new_users_30d
    if can_appts:
        recent_30d["new_appointments"] = new_appts_30d
        recent_30d["completed"] = completed_30d
    if can_queries:  recent_30d["open_queries"] = open_queries


    payload: dict = {
        "totals": totals,
        "recent_30d": recent_30d,
        # Echo the resolved window so the frontend can display "From / To"
        # in the filter UI without re-deriving it.
        "window": {
            "date_from": window_start.date().isoformat(),
            "date_to": window_end.date().isoformat(),
        },
    }


    # Charts gated by section. Frontend tolerates missing keys (already gated
    # on the dashboard), but absent sections also save bytes for moderators.
    if can_appts:
        payload["appointment_by_status"] = appointment_by_status
        payload["appointment_by_payment"] = appointment_by_payment
        payload["appointments_per_month"] = appointments_per_month
    if can_users:
        payload["users_per_month"] = users_per_month


    # Financial data is super-admin-only.
    if super_:
        payload["revenue"] = {
            "total": total_revenue,
            "total_completed": total_completed,
            "total_hours": round(total_hours, 1),
            "fee_per_consultation": fee,
        }
        payload["revenue_per_month"] = revenue_per_month


    return payload
