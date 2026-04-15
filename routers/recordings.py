from fastapi import APIRouter, Depends, HTTPException, Form
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Optional

from database import get_db
import models
import schemas
from utils.auth import get_current_user, require_admin
from utils.audit import log_action

router = APIRouter(tags=["recordings"])


# ── Pydantic models ──────────────────────────────────────────────────────────

class BulkAssignRecordingRequest(BaseModel):
    title: str
    recording_url: str
    user_ids: list[int] = []
    list_ids: list[int] = []     # user list IDs — members are resolved server-side


class BulkAssignRecordingResponse(BaseModel):
    assigned_count: int
    skipped: list[str] = []


# ── Admin: add recording to a single user ────────────────────────────────────

@router.post("/admin/recordings", response_model=schemas.RecordingOut)
def add_recording(
    user_id: int = Form(...),
    title: str = Form(...),
    zoom_recording_url: str = Form(...),
    appointment_id: Optional[int] = Form(None),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    rec = models.Recording(
        user_id=user_id,
        appointment_id=appointment_id,
        title=title,
        zoom_recording_url=zoom_recording_url,
    )
    db.add(rec)
    db.commit()
    db.refresh(rec)
    return rec


# ── Admin: bulk assign recording to users + user lists ───────────────────────

@router.post("/admin/recordings/bulk-assign", response_model=BulkAssignRecordingResponse)
def bulk_assign_recording(
    data: BulkAssignRecordingRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not data.title or not data.recording_url:
        raise HTTPException(status_code=400, detail="Title and recording URL are required")

    # Collect all user IDs from explicit list + user lists
    all_user_ids = set(data.user_ids)

    for list_id in data.list_ids:
        user_list = db.query(models.UserList).filter(models.UserList.id == list_id).first()
        if user_list:
            members = db.query(models.UserListMember).filter(
                models.UserListMember.user_list_id == list_id
            ).all()
            for m in members:
                all_user_ids.add(m.user_id)

    if not all_user_ids:
        raise HTTPException(status_code=400, detail="No users selected")

    assigned = 0
    skipped: list[str] = []

    for uid in all_user_ids:
        user = db.query(models.User).filter(models.User.id == uid).first()
        if not user:
            skipped.append(f"User #{uid}: not found")
            continue
        rec = models.Recording(
            user_id=uid,
            title=data.title,
            zoom_recording_url=data.recording_url,
        )
        db.add(rec)
        assigned += 1

    db.commit()
    log_action(db, admin.id, "bulk_assign_recording", "recording", 0,
               f"'{data.title}' to {assigned} users")
    return {"assigned_count": assigned, "skipped": skipped}


# ── Admin: delete recording ──────────────────────────────────────────────────

@router.delete("/admin/recordings/{rec_id}")
def delete_recording(
    rec_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rec = db.query(models.Recording).filter(models.Recording.id == rec_id).first()
    if not rec:
        raise HTTPException(status_code=404, detail="Recording not found")
    title = rec.title
    db.delete(rec)
    db.commit()
    log_action(db, admin.id, "delete_recording", "recording", rec_id, f"'{title}'")
    return {"message": "Recording deleted"}


# ── Admin: all recordings ────────────────────────────────────────────────────

@router.get("/admin/recordings", response_model=List[schemas.RecordingOut])
def admin_all_recordings(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return db.query(models.Recording).order_by(models.Recording.created_at.desc()).all()


# ── User: my recordings ─────────────────────────────────────────────────────

@router.get("/recordings/my", response_model=List[schemas.RecordingOut])
def my_recordings(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Recording)
        .filter(models.Recording.user_id == user.id)
        .order_by(models.Recording.created_at.desc())
        .all()
    )
