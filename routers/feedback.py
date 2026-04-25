"""
Post-consultation feedback collection.
- Users submit a 1-5 star rating + optional comment for their completed appointments.
- One feedback per appointment (enforced by appointment_id uniqueness).
- Admin endpoint returns all feedback with appointment + user context for review.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db
import models
from utils.auth import get_current_user, require_admin

router = APIRouter(tags=["feedback"])


class FeedbackIn(BaseModel):
    rating: int = Field(..., ge=1, le=5)
    comment: Optional[str] = None


class FeedbackOut(BaseModel):
    id: int
    appointment_id: int
    user_id: int
    rating: int
    comment: Optional[str]
    created_at: Optional[str]

    class Config:
        from_attributes = True


@router.post("/appointments/{appt_id}/feedback")
def submit_feedback(
    appt_id: int,
    data: FeedbackIn,
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
    if appt.status != "completed":
        raise HTTPException(status_code=400, detail="Feedback is only available after the consultation is completed")

    existing = (
        db.query(models.ConsultationFeedback)
        .filter(models.ConsultationFeedback.appointment_id == appt_id)
        .first()
    )
    if existing:
        existing.rating = data.rating
        existing.comment = (data.comment or "").strip() or None
    else:
        existing = models.ConsultationFeedback(
            appointment_id=appt_id,
            user_id=user.id,
            rating=data.rating,
            comment=(data.comment or "").strip() or None,
        )
        db.add(existing)
    db.commit()
    db.refresh(existing)
    return {"message": "Feedback recorded", "id": existing.id}


@router.get("/appointments/{appt_id}/feedback")
def get_my_feedback(
    appt_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    fb = (
        db.query(models.ConsultationFeedback)
        .filter(
            models.ConsultationFeedback.appointment_id == appt_id,
            models.ConsultationFeedback.user_id == user.id,
        )
        .first()
    )
    if not fb:
        return None
    return {
        "id": fb.id,
        "rating": fb.rating,
        "comment": fb.comment,
        "created_at": fb.created_at.isoformat() if fb.created_at else None,
    }


@router.get("/admin/feedback")
def list_all_feedback(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(models.ConsultationFeedback)
        .order_by(desc(models.ConsultationFeedback.created_at))
        .limit(500)
        .all()
    )
    out = []
    for f in rows:
        appt = db.query(models.Appointment).filter(models.Appointment.id == f.appointment_id).first()
        out.append({
            "id": f.id,
            "rating": f.rating,
            "comment": f.comment,
            "created_at": f.created_at.isoformat() if f.created_at else None,
            "appointment_id": f.appointment_id,
            "user_name": appt.name if appt else None,
            "scheduled_date": appt.scheduled_date if appt else None,
            "scheduled_time": appt.scheduled_time if appt else None,
        })
    return out


@router.get("/admin/feedback/summary")
def feedback_summary(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    total = db.query(func.count(models.ConsultationFeedback.id)).scalar() or 0
    avg = db.query(func.avg(models.ConsultationFeedback.rating)).scalar()
    by_rating_rows = (
        db.query(models.ConsultationFeedback.rating, func.count(models.ConsultationFeedback.id))
        .group_by(models.ConsultationFeedback.rating)
        .all()
    )
    return {
        "total": total,
        "average": round(float(avg), 2) if avg is not None else None,
        "by_rating": [{"rating": r, "count": c} for r, c in by_rating_rows],
    }
