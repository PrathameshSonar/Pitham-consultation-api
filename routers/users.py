from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import List, Optional

from database import get_db
import models
import schemas
from pydantic import BaseModel
from utils.auth import require_admin, require_super_admin
from utils.audit import log_action
from utils.permissions import ADMIN_SECTIONS, get_user_permissions, serialize_permissions, require_section

router = APIRouter(prefix="/admin/users", tags=["admin-users"])

# Section gate for endpoints that surface full PII (DOB/TOB/birth_place).
# Cross-section name resolution goes through /admin/users/lookup which only
# requires require_admin and returns a minimal projection.
_section_admin = require_section("users")

# Cap on how many moderators can exist at once.
MAX_MODERATORS = 20


# ── Lookup: minimal user info, available to any admin/moderator ──────────────
#
# This endpoint is intentionally above the section-gated ones so other admin
# panels (docs, recordings, broadcasts, user-lists, appointments) can resolve
# user names/emails for display without granting "users" permission, which
# would also expose sensitive astrology consultation data (DOB/TOB/birth place).

@router.get("/lookup", response_model=List[schemas.UserLookupOut])
def lookup_users(
    ids: Optional[str] = Query(None, description="Comma-separated user ids"),
    search: Optional[str] = Query(None, min_length=2),
    limit: int = Query(50, ge=1, le=200),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Cross-section name/email lookup. Either pass `ids=1,2,3` to resolve
    specific users, or `search=foo` for a name/email/city prefix match. Always
    returns only id+name+email+mobile+city+state — never DOB/TOB/birth_place."""
    q = db.query(models.User).filter(models.User.role == "user")
    if ids:
        # Tolerate stray spaces / non-digits — never blow up on bad input.
        try:
            id_list = [int(x) for x in ids.split(",") if x.strip().isdigit()]
        except ValueError:
            id_list = []
        if not id_list:
            return []
        q = q.filter(models.User.id.in_(id_list[:limit]))
        return q.all()
    if search:
        term = f"%{search.strip()}%"
        q = q.filter(
            (models.User.name.ilike(term))
            | (models.User.email.ilike(term))
            | (models.User.city.ilike(term))
        )
        return q.order_by(models.User.name.asc()).limit(limit).all()
    # No filter — return most recent N (cheap default for UI prefetch).
    return q.order_by(models.User.created_at.desc()).limit(limit).all()


# ── Section-gated endpoints — full PII ────────────────────────────────────────


@router.get("", response_model=List[schemas.UserOut])
def list_users(
    search: Optional[str] = Query(None),
    city: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    role: Optional[str] = Query(None, pattern="^(user|moderator|admin)$"),
    admin: models.User = Depends(_section_admin),
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
    admin: models.User = Depends(_section_admin),
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
    admin: models.User = Depends(_section_admin),
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
    admin: models.User = Depends(_section_admin),
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
    permissions: Optional[list[str]] = None  # only meaningful when promoting to moderator


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
    # When promoting to moderator, apply requested permissions (or grant full set
    # if none specified — least surprising default for the super admin). When
    # demoting away from moderator, blank out the permissions so a future
    # promotion has to re-grant them deliberately.
    if data.role == "moderator":
        if data.permissions is not None:
            user.permissions = serialize_permissions(data.permissions)
        elif old_role != "moderator":
            user.permissions = serialize_permissions(ADMIN_SECTIONS)
    else:
        user.permissions = "[]"

    db.commit()
    log_action(db, admin.id, "change_role", "user", user_id,
               f"{user.name}: {old_role} -> {data.role}")
    return {"message": f"Role changed to {data.role}"}


# ── Section permissions for an existing moderator ────────────────────────────

class UpdatePermissionsRequest(BaseModel):
    permissions: list[str]


@router.put("/{user_id}/permissions")
def update_user_permissions(
    user_id: int,
    data: UpdatePermissionsRequest,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Set the section permissions for a moderator. Caller must be super admin.
    Unknown section keys are silently dropped server-side, so a stale frontend
    can never grant access to a section that doesn't exist."""
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role != "moderator":
        raise HTTPException(
            status_code=400,
            detail="Permissions can only be set on moderators. Promote the user first.",
        )

    old = get_user_permissions(user)
    user.permissions = serialize_permissions(data.permissions)
    db.commit()
    log_action(
        db, admin.id, "update_permissions", "user", user_id,
        f"{user.name}: {sorted(old)} -> {sorted(get_user_permissions(user))}",
    )
    return {"message": "Permissions updated", "permissions": get_user_permissions(user)}


@router.get("/moderators/count")
def moderator_count(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    """Return current moderator count and the configured maximum."""
    current = db.query(models.User).filter(models.User.role == "moderator").count()
    return {"current": current, "max": MAX_MODERATORS}
