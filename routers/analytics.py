from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func, extract
from datetime import datetime, timedelta

from database import get_db
import models
from utils.auth import require_admin

router = APIRouter(prefix="/admin/analytics", tags=["analytics"])


@router.get("")
def get_analytics(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    now = datetime.utcnow()
    month_ago = now - timedelta(days=30)

    # ── Totals ──
    total_users = db.query(models.User).filter(models.User.role == "user").count()
    total_appointments = db.query(models.Appointment).count()
    total_documents = db.query(models.Document).filter(models.Document.user_id.is_not(None)).count()
    total_recordings = db.query(models.Recording).count()
    total_queries = db.query(models.Query).count()

    # ── Appointment status breakdown (for pie chart) ──
    status_counts = (
        db.query(models.Appointment.status, func.count(models.Appointment.id))
        .group_by(models.Appointment.status)
        .all()
    )
    appointment_by_status = [{"status": s, "count": c} for s, c in status_counts]

    # ── Payment breakdown (for pie chart) ──
    payment_counts = (
        db.query(models.Appointment.payment_status, func.count(models.Appointment.id))
        .group_by(models.Appointment.payment_status)
        .all()
    )
    appointment_by_payment = [{"status": s, "count": c} for s, c in payment_counts]

    # ── Appointments per month (last 6 months, for bar chart) ──
    six_months_ago = now - timedelta(days=180)
    monthly_raw = (
        db.query(
            func.date_format(models.Appointment.created_at, "%Y-%m").label("month"),
            func.count(models.Appointment.id),
        )
        .filter(models.Appointment.created_at >= six_months_ago)
        .group_by("month")
        .order_by("month")
        .all()
    )
    # Fallback for SQLite which doesn't have date_format
    if not monthly_raw:
        monthly_raw = (
            db.query(
                func.strftime("%Y-%m", models.Appointment.created_at).label("month"),
                func.count(models.Appointment.id),
            )
            .filter(models.Appointment.created_at >= six_months_ago)
            .group_by("month")
            .order_by("month")
            .all()
        )
    appointments_per_month = [{"month": m, "count": c} for m, c in monthly_raw]

    # ── New users per month (last 6 months) ──
    users_monthly_raw = (
        db.query(
            func.date_format(models.User.created_at, "%Y-%m").label("month"),
            func.count(models.User.id),
        )
        .filter(models.User.created_at >= six_months_ago, models.User.role == "user")
        .group_by("month")
        .order_by("month")
        .all()
    )
    if not users_monthly_raw:
        users_monthly_raw = (
            db.query(
                func.strftime("%Y-%m", models.User.created_at).label("month"),
                func.count(models.User.id),
            )
            .filter(models.User.created_at >= six_months_ago, models.User.role == "user")
            .group_by("month")
            .order_by("month")
            .all()
        )
    users_per_month = [{"month": m, "count": c} for m, c in users_monthly_raw]

    # ── Recent activity (last 30 days) ──
    new_users_30d = db.query(models.User).filter(
        models.User.created_at >= month_ago, models.User.role == "user"
    ).count()
    new_appts_30d = db.query(models.Appointment).filter(
        models.Appointment.created_at >= month_ago
    ).count()
    completed_30d = db.query(models.Appointment).filter(
        models.Appointment.status == "completed",
        models.Appointment.updated_at >= month_ago,
    ).count()
    open_queries = db.query(models.Query).filter(models.Query.status == "open").count()

    # ── Revenue & consultation time ──
    fee_row = db.query(models.SiteSetting).filter(models.SiteSetting.key == "consultation_fee").first()
    fee = int(fee_row.value) if fee_row else 500

    total_completed = db.query(models.Appointment).filter(
        models.Appointment.status == "completed"
    ).count()
    total_revenue = total_completed * fee
    total_hours = (total_completed * 30) / 60  # avg 30 min per consultation

    # Monthly revenue (last 6 months)
    completed_monthly_raw = (
        db.query(
            func.date_format(models.Appointment.updated_at, "%Y-%m").label("month"),
            func.count(models.Appointment.id),
        )
        .filter(
            models.Appointment.status == "completed",
            models.Appointment.updated_at >= six_months_ago,
        )
        .group_by("month")
        .order_by("month")
        .all()
    )
    if not completed_monthly_raw:
        completed_monthly_raw = (
            db.query(
                func.strftime("%Y-%m", models.Appointment.updated_at).label("month"),
                func.count(models.Appointment.id),
            )
            .filter(
                models.Appointment.status == "completed",
                models.Appointment.updated_at >= six_months_ago,
            )
            .group_by("month")
            .order_by("month")
            .all()
        )
    revenue_per_month = [{"month": m, "count": c, "revenue": c * fee} for m, c in completed_monthly_raw]

    return {
        "totals": {
            "users": total_users,
            "appointments": total_appointments,
            "documents": total_documents,
            "recordings": total_recordings,
            "queries": total_queries,
        },
        "recent_30d": {
            "new_users": new_users_30d,
            "new_appointments": new_appts_30d,
            "completed": completed_30d,
            "open_queries": open_queries,
        },
        "appointment_by_status": appointment_by_status,
        "appointment_by_payment": appointment_by_payment,
        "appointments_per_month": appointments_per_month,
        "users_per_month": users_per_month,
        "revenue": {
            "total": total_revenue,
            "total_completed": total_completed,
            "total_hours": round(total_hours, 1),
            "fee_per_consultation": fee,
        },
        "revenue_per_month": revenue_per_month,
    }
