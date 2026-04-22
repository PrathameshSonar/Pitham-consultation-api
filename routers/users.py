from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
import models
import schemas
from pydantic import BaseModel
from utils.auth import require_admin, require_super_admin
from utils.audit import log_action

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

# Cap on how many moderators can exist at once.
MAX_MODERATORS = 5


@router.get("", response_model=List[schemas.UserOut])
def list_users(
    search: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    role: Optional[str] = Query(None, pattern="^(user|moderator|admin)$"),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.User).filter(models.User.role == (role or "user"))

    if search:
        q = q.filter(models.User.name.ilike(f"%{search}%"))
    if city:
        q = q.filter(models.User.city.ilike(f"%{city}%"))
    if state:
        q = q.filter(models.User.state.ilike(f"%{state}%"))
    if country:
        q = q.filter(models.User.country.ilike(f"%{country}%"))

    return q.order_by(models.User.created_at.desc()).all()


@router.get("/{user_id}", response_model=schemas.UserOut)
def get_user(
    user_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("/{user_id}/appointments", response_model=List[schemas.AppointmentOut])
def user_appointments(
    user_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Appointment)
        .filter(models.Appointment.user_id == user_id)
        .order_by(models.Appointment.created_at.desc())
        .all()
    )


@router.get("/{user_id}/documents", response_model=List[schemas.DocumentOut])
def user_documents(
    user_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Document)
        .filter(models.Document.user_id == user_id)
        .order_by(models.Document.created_at.desc())
        .all()
    )


# ── Super Admin: manage roles ────────────────────────────────────────────────

class ChangeRoleRequest(BaseModel):
    role: str  # "user" | "moderator" | "admin"


@router.put("/{user_id}/role")
def change_user_role(
    user_id: int,
    data: ChangeRoleRequest,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if data.role not in ("user", "moderator", "admin"):
        raise HTTPException(status_code=400, detail="Role must be user, moderator, or admin")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    # Protect existing admins — only an admin can demote another admin via DB directly.
    if user.role == "admin" and data.role != "admin":
        raise HTTPException(status_code=400, detail="Cannot change another admin's role")

    old_role = user.role

    # Enforce moderator cap when promoting INTO moderator
    if data.role == "moderator" and old_role != "moderator":
        current = (
            db.query(models.User)
            .filter(models.User.role == "moderator")
            .count()
        )
        if current >= MAX_MODERATORS:
            raise HTTPException(
                status_code=400,
                detail=f"Moderator limit reached ({MAX_MODERATORS}). Revoke an existing moderator first.",
            )

    user.role = data.role
    db.commit()
    log_action(db, admin.id, "change_role", "user", user_id,
               f"{user.name}: {old_role} -> {data.role}")
    return {"message": f"Role changed to {data.role}"}


@router.get("/moderators/count")
def moderator_count(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Return current moderator count and the configured maximum."""
    current = db.query(models.User).filter(models.User.role == "moderator").count()
    return {"current": current, "max": MAX_MODERATORS}
