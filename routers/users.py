from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
import models
import schemas
from pydantic import BaseModel
from utils.auth import require_admin, require_super_admin
from utils.audit import log_action

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


@router.get("", response_model=List[schemas.UserOut])
def list_users(
    search: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.User).filter(models.User.role == "user")

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
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Role must be user, moderator, or admin")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == admin.id:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    old_role = user.role
    user.role = data.role
    db.commit()
    log_action(db, admin.id, "change_role", "user", user_id,
               f"{user.name}: {old_role} -> {data.role}")
    return {"message": f"Role changed to {data.role}"}
