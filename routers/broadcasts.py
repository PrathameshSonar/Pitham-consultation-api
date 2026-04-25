"""Broadcast notifications.

Admin OR moderator can send to all users or a specific user list. Each user
sees the message in /dashboard/notifications and can mark it read.
"""

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.orm import Session

from database import get_db
import models
import schemas
from utils.auth import get_current_user, require_admin
from utils.audit import log_action
from utils.storage import storage
from utils.uploads import IMAGE_MIMES, validate_upload, check_size, safe_filename
from utils.email import send_email
from utils.whatsapp import send_whatsapp

logger = logging.getLogger("pitham.broadcasts")
router = APIRouter(tags=["broadcasts"])

MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB


def _broadcast_recipients(db: Session, b: models.Broadcast) -> list[models.User]:
    """Resolve a broadcast's target to actual User rows."""
    q = db.query(models.User).filter(models.User.is_active.is_(True), models.User.role == "user")
    if b.target_type == "list" and b.target_list_id:
        member_ids = (
            db.query(models.UserListMember.user_id)
            .filter(models.UserListMember.user_list_id == b.target_list_id)
            .subquery()
        )
        q = q.filter(models.User.id.in_(select(member_ids)))
    return q.all()


def _serialize(b: models.Broadcast, sender_name: str | None = None, is_read: bool = False) -> dict:
    return {
        "id": b.id,
        "title": b.title,
        "message": b.message,
        "image_path": b.image_path,
        "target_type": b.target_type,
        "target_list_id": b.target_list_id,
        "sent_by_name": sender_name,
        "created_at": b.created_at,
        "is_read": is_read,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin / moderator — create + list + delete
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/admin/broadcasts", response_model=schemas.BroadcastOut)
async def create_broadcast(
    title: str = Form(...),
    message: str = Form(...),
    target_type: str = Form("all"),                 # "all" | "list"
    target_list_id: Optional[int] = Form(None),
    image: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if target_type not in ("all", "list"):
        raise HTTPException(status_code=400, detail="target_type must be 'all' or 'list'")
    if target_type == "list" and not target_list_id:
        raise HTTPException(status_code=400, detail="target_list_id is required when target_type='list'")
    if not title.strip() or not message.strip():
        raise HTTPException(status_code=400, detail="Title and message are required")

    image_path = None
    if image and image.filename:
        validate_upload(image, IMAGE_MIMES, MAX_IMAGE_SIZE, label="Broadcast image")
        content = await image.read()
        check_size(content, MAX_IMAGE_SIZE, label="Broadcast image")
        import uuid
        fname = safe_filename(f"broadcast_{uuid.uuid4().hex[:10]}.{(image.filename or 'jpg').rsplit('.', 1)[-1].lower()}")
        image_path = await storage.save("broadcasts", fname, content)

    b = models.Broadcast(
        title=title.strip(),
        message=message.strip(),
        image_path=image_path,
        target_type=target_type,
        target_list_id=target_list_id,
        sent_by=admin.id,
    )
    db.add(b); db.commit(); db.refresh(b)
    log_action(db, admin.id, "send_broadcast", "broadcast", b.id, f"target={target_type}")

    # Fan-out via email + WhatsApp (each safe-no-op when not configured / no recipient)
    try:
        recipients = _broadcast_recipients(db, b)
        plain = f"{b.title}\n\n{b.message}"
        for u in recipients:
            if u.email:
                send_email(
                    u.email,
                    f"📢 {b.title}",
                    f"<h3>{b.title}</h3><p style='line-height:1.7'>{b.message}</p>",
                )
            if u.mobile:
                send_whatsapp(u.mobile, plain)
    except Exception as e:
        logger.error("Broadcast fan-out failed (notification stored): %s", e)

    return _serialize(b, admin.name, is_read=False)


@router.get("/admin/broadcasts", response_model=List[schemas.BroadcastOut])
def list_admin_broadcasts(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = db.query(models.Broadcast).order_by(models.Broadcast.created_at.desc()).limit(200).all()
    senders = {u.id: u.name for u in db.query(models.User).filter(
        models.User.id.in_({r.sent_by for r in rows})
    ).all()} if rows else {}
    return [_serialize(b, senders.get(b.sent_by)) for b in rows]


@router.delete("/admin/broadcasts/{bid}")
def delete_broadcast(
    bid: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    b = db.query(models.Broadcast).filter(models.Broadcast.id == bid).first()
    if not b:
        raise HTTPException(status_code=404, detail="Broadcast not found")
    storage.delete(b.image_path)
    db.query(models.BroadcastRead).filter(models.BroadcastRead.broadcast_id == bid).delete()
    title = b.title
    db.delete(b); db.commit()
    log_action(db, admin.id, "delete_broadcast", "broadcast", bid, title)
    return {"message": "Deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# User — see what was sent to me + mark read
# ─────────────────────────────────────────────────────────────────────────────

def _broadcasts_for_user(db: Session, user_id: int) -> list[models.Broadcast]:
    """All broadcasts whose target includes this user."""
    list_ids = [
        row[0] for row in db.query(models.UserListMember.user_list_id)
        .filter(models.UserListMember.user_id == user_id).all()
    ]
    q = db.query(models.Broadcast).filter(
        (models.Broadcast.target_type == "all")
        | ((models.Broadcast.target_type == "list") & models.Broadcast.target_list_id.in_(list_ids))
    )
    return q.order_by(models.Broadcast.created_at.desc()).limit(100).all()


@router.get("/broadcasts/my", response_model=List[schemas.BroadcastOut])
def list_my_broadcasts(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = _broadcasts_for_user(db, user.id)
    if not rows:
        return []
    read_ids = {
        r.broadcast_id for r in db.query(models.BroadcastRead.broadcast_id)
        .filter(models.BroadcastRead.user_id == user.id,
                models.BroadcastRead.broadcast_id.in_([b.id for b in rows]))
        .all()
    }
    senders = {u.id: u.name for u in db.query(models.User).filter(
        models.User.id.in_({r.sent_by for r in rows})
    ).all()}
    return [_serialize(b, senders.get(b.sent_by), is_read=(b.id in read_ids)) for b in rows]


@router.get("/broadcasts/unread-count", response_model=schemas.UnreadCount)
def unread_count(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = _broadcasts_for_user(db, user.id)
    if not rows:
        return {"count": 0}
    read_ids = {
        r.broadcast_id for r in db.query(models.BroadcastRead.broadcast_id)
        .filter(models.BroadcastRead.user_id == user.id,
                models.BroadcastRead.broadcast_id.in_([b.id for b in rows]))
        .all()
    }
    return {"count": sum(1 for b in rows if b.id not in read_ids)}


@router.post("/broadcasts/{bid}/read")
def mark_read(
    bid: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    # Idempotent — re-marking is fine
    exists = db.query(models.BroadcastRead).filter(
        models.BroadcastRead.broadcast_id == bid,
        models.BroadcastRead.user_id == user.id,
    ).first()
    if not exists:
        db.add(models.BroadcastRead(broadcast_id=bid, user_id=user.id))
        db.commit()
    return {"message": "Marked read"}


@router.post("/broadcasts/read-all")
def mark_all_read(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = _broadcasts_for_user(db, user.id)
    if not rows:
        return {"count": 0}
    existing = {
        r.broadcast_id for r in db.query(models.BroadcastRead.broadcast_id)
        .filter(models.BroadcastRead.user_id == user.id,
                models.BroadcastRead.broadcast_id.in_([b.id for b in rows])).all()
    }
    new_reads = [models.BroadcastRead(broadcast_id=b.id, user_id=user.id) for b in rows if b.id not in existing]
    if new_reads:
        db.bulk_save_objects(new_reads)
        db.commit()
    return {"count": len(new_reads)}
