"""
Pitham upcoming events — public read, admin (super admin only) CRUD.
Image can be supplied as an external URL OR uploaded as a file.
"""

import os
import re
import uuid
from datetime import date, datetime
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form
from sqlalchemy.orm import Session

from database import get_db
import models
import schemas
from utils.auth import require_super_admin
from utils.audit import log_action
from utils.uploads import IMAGE_MIMES, validate_upload, check_size

router = APIRouter(tags=["events"])

EVENT_UPLOAD_DIR = "uploads/pitham"
os.makedirs(EVENT_UPLOAD_DIR, exist_ok=True)
MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", os.path.basename(name or "img"))


async def _save_event_image(file: UploadFile) -> str:
    validate_upload(file, IMAGE_MIMES, MAX_IMAGE_SIZE, label="Event image")
    content = await file.read()
    check_size(content, MAX_IMAGE_SIZE, label="Event image")
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    fname = _safe_filename(f"event_{uuid.uuid4().hex[:10]}{ext}")
    path = os.path.join(EVENT_UPLOAD_DIR, fname)
    async with aiofiles.open(path, "wb") as f:
        await f.write(content)
    return path


def _delete_file(path: Optional[str]):
    if not path or path.startswith(("http://", "https://")):
        return
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ── Public: list upcoming events ─────────────────────────────────────────────

@router.get("/events", response_model=List[schemas.EventOut])
def list_events(
    scope: str = Query("upcoming", pattern="^(upcoming|past|all)$"),
    limit: Optional[int] = Query(None, ge=1, le=100),
    db: Session = Depends(get_db),
):
    today = date.today().isoformat()
    q = db.query(models.Event)
    if scope == "upcoming":
        q = q.filter(models.Event.event_date >= today).order_by(
            models.Event.is_featured.desc(), models.Event.event_date.asc()
        )
    elif scope == "past":
        q = q.filter(models.Event.event_date < today).order_by(models.Event.event_date.desc())
    else:
        q = q.order_by(models.Event.event_date.desc())
    if limit:
        q = q.limit(limit)
    return q.all()


@router.get("/events/{event_id}", response_model=schemas.EventOut)
def get_event(event_id: int, db: Session = Depends(get_db)):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


# ── Admin: create (multipart so image can be uploaded) ───────────────────────

@router.post("/admin/events", response_model=schemas.EventOut)
async def admin_create_event(
    title: str = Form(...),
    event_date: str = Form(...),
    description: str = Form(""),
    event_time: str = Form(""),
    location: str = Form(""),
    image_url: str = Form(""),
    is_featured: bool = Form(False),
    image: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if not title.strip():
        raise HTTPException(status_code=400, detail="Title is required")
    if not event_date.strip():
        raise HTTPException(status_code=400, detail="Event date is required")

    # Uploaded file wins over image_url text
    final_image: Optional[str] = None
    if image and image.filename:
        final_image = await _save_event_image(image)
    elif image_url.strip():
        final_image = image_url.strip()

    event = models.Event(
        title=title.strip(),
        description=description.strip() or None,
        event_date=event_date,
        event_time=event_time.strip() or None,
        location=location.strip() or None,
        image_url=final_image,
        is_featured=is_featured,
        created_by=admin.id,
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    log_action(db, admin.id, "create_event", "event", event.id, event.title)
    return event


# ── Admin: list all (including past) ─────────────────────────────────────────

@router.get("/admin/events", response_model=List[schemas.EventOut])
def admin_list_events(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Event)
        .order_by(models.Event.is_featured.desc(), models.Event.event_date.desc())
        .all()
    )


# ── Admin: update ────────────────────────────────────────────────────────────

@router.put("/admin/events/{event_id}", response_model=schemas.EventOut)
async def admin_update_event(
    event_id: int,
    title: Optional[str] = Form(None),
    event_date: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    event_time: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    image_url: Optional[str] = Form(None),
    is_featured: Optional[bool] = Form(None),
    image: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")

    if title is not None and title.strip():
        event.title = title.strip()
    if event_date is not None and event_date.strip():
        event.event_date = event_date
    if description is not None:
        event.description = description.strip() or None
    if event_time is not None:
        event.event_time = event_time.strip() or None
    if location is not None:
        event.location = location.strip() or None
    if is_featured is not None:
        event.is_featured = is_featured

    # Image: file upload wins; otherwise plain url field replaces if non-empty
    if image and image.filename:
        new_path = await _save_event_image(image)
        _delete_file(event.image_url)
        event.image_url = new_path
    elif image_url is not None:
        url = image_url.strip()
        if url and url != event.image_url:
            _delete_file(event.image_url)
            event.image_url = url
        elif not url:
            _delete_file(event.image_url)
            event.image_url = None

    event.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(event)
    log_action(db, admin.id, "update_event", "event", event.id, event.title)
    return event


# ── Admin: delete ────────────────────────────────────────────────────────────

@router.delete("/admin/events/{event_id}")
def admin_delete_event(
    event_id: int,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    _delete_file(event.image_url)
    title = event.title
    db.delete(event)
    db.commit()
    log_action(db, admin.id, "delete_event", "event", event_id, title)
    return {"message": "Event deleted"}
