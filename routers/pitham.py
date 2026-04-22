"""
Pitham public-page CMS — banners, testimonials, videos/podcasts, instagram posts,
and consolidated public fetch.

All admin endpoints require super admin (moderators cannot edit Pitham content).
"""

import os
import re
import uuid
from datetime import date
from typing import List, Optional

import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form, Query
from sqlalchemy.orm import Session

from database import get_db
import models
import schemas
from utils.auth import require_super_admin
from utils.audit import log_action
from utils.uploads import IMAGE_MIMES, validate_upload, check_size, safe_filename as _shared_safe

router = APIRouter(tags=["pitham"])

UPLOAD_DIR = "uploads/pitham"
os.makedirs(UPLOAD_DIR, exist_ok=True)

MAX_IMAGE_SIZE = 8 * 1024 * 1024  # 8MB per image
ALLOWED_KINDS = {"banner", "video", "instagram", "gallery"}


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w.\-]", "_", os.path.basename(name or "img"))


async def _save_upload(file: UploadFile, prefix: str) -> str:
    validate_upload(file, IMAGE_MIMES, MAX_IMAGE_SIZE, label="Image")
    content = await file.read()
    check_size(content, MAX_IMAGE_SIZE, label="Image")
    ext = os.path.splitext(file.filename or "")[1].lower() or ".jpg"
    fname = f"{prefix}_{uuid.uuid4().hex[:10]}{ext}"
    path = os.path.join(UPLOAD_DIR, _safe_filename(fname))
    async with aiofiles.open(path, "wb") as f:
        await f.write(content)
    return path


def _delete_file(path: Optional[str]):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Public — single bundle fetch for the /pitham page
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/pitham/cms", response_model=schemas.PithamCmsBundle)
def public_cms_bundle(db: Session = Depends(get_db)):
    today = date.today().isoformat()

    def _media(kind: str):
        return (
            db.query(models.PithamMedia)
            .filter(models.PithamMedia.kind == kind, models.PithamMedia.is_active.is_(True))
            .order_by(models.PithamMedia.sort_order.asc(), models.PithamMedia.created_at.desc())
            .all()
        )

    return {
        "banners": _media("banner"),
        "videos": _media("video"),
        "instagram": _media("instagram"),
        "gallery": _media("gallery"),
        "testimonials": (
            db.query(models.Testimonial)
            .filter(models.Testimonial.is_active.is_(True))
            .order_by(models.Testimonial.sort_order.asc(), models.Testimonial.created_at.desc())
            .all()
        ),
        "featured_events": (
            db.query(models.Event)
            .filter(models.Event.is_featured.is_(True), models.Event.event_date >= today)
            .order_by(models.Event.event_date.asc())
            .limit(5)
            .all()
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Pitham Media (banners / videos / instagram)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/pitham/media", response_model=List[schemas.PithamMediaOut])
def admin_list_media(
    kind: Optional[str] = Query(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    q = db.query(models.PithamMedia)
    if kind:
        if kind not in ALLOWED_KINDS:
            raise HTTPException(status_code=400, detail="Invalid media kind")
        q = q.filter(models.PithamMedia.kind == kind)
    return q.order_by(models.PithamMedia.sort_order.asc(), models.PithamMedia.created_at.desc()).all()


@router.post("/admin/pitham/media", response_model=schemas.PithamMediaOut)
async def admin_create_media(
    kind: str = Form(...),
    title: str = Form(""),
    url: str = Form(""),
    sort_order: int = Form(0),
    is_active: bool = Form(True),
    image: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if kind not in ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="Invalid media kind")

    image_path: Optional[str] = None
    if image and image.filename:
        image_path = await _save_upload(image, prefix=kind)

    # Validation per kind
    if kind in ("banner", "gallery") and not image_path:
        raise HTTPException(status_code=400, detail="An image upload is required")
    if kind in ("video", "instagram") and not (url or "").strip():
        raise HTTPException(status_code=400, detail="URL is required")

    item = models.PithamMedia(
        kind=kind,
        title=title.strip() or None,
        url=url.strip() or None,
        image_path=image_path,
        sort_order=sort_order,
        is_active=is_active,
        created_by=admin.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    log_action(db, admin.id, "create_pitham_media", "pitham_media", item.id, kind)
    return item


@router.put("/admin/pitham/media/{item_id}", response_model=schemas.PithamMediaOut)
async def admin_update_media(
    item_id: int,
    title: Optional[str] = Form(None),
    url: Optional[str] = Form(None),
    sort_order: Optional[int] = Form(None),
    is_active: Optional[bool] = Form(None),
    image: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    item = db.query(models.PithamMedia).filter(models.PithamMedia.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Media not found")

    if title is not None:
        item.title = title.strip() or None
    if url is not None:
        item.url = url.strip() or None
    if sort_order is not None:
        item.sort_order = sort_order
    if is_active is not None:
        item.is_active = is_active
    if image and image.filename:
        new_path = await _save_upload(image, prefix=item.kind)
        _delete_file(item.image_path)
        item.image_path = new_path

    db.commit()
    db.refresh(item)
    log_action(db, admin.id, "update_pitham_media", "pitham_media", item.id, item.kind)
    return item


@router.delete("/admin/pitham/media/{item_id}")
def admin_delete_media(
    item_id: int,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    item = db.query(models.PithamMedia).filter(models.PithamMedia.id == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Media not found")
    _delete_file(item.image_path)
    kind = item.kind
    db.delete(item)
    db.commit()
    log_action(db, admin.id, "delete_pitham_media", "pitham_media", item_id, kind)
    return {"message": "Deleted"}


# ─────────────────────────────────────────────────────────────────────────────
# Admin — Testimonials
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/admin/pitham/testimonials", response_model=List[schemas.TestimonialOut])
def admin_list_testimonials(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Testimonial)
        .order_by(models.Testimonial.sort_order.asc(), models.Testimonial.created_at.desc())
        .all()
    )


@router.post("/admin/pitham/testimonials", response_model=schemas.TestimonialOut)
async def admin_create_testimonial(
    name: str = Form(...),
    quote: str = Form(...),
    location: str = Form(""),
    sort_order: int = Form(0),
    is_active: bool = Form(True),
    photo: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    if not name.strip() or not quote.strip():
        raise HTTPException(status_code=400, detail="Name and quote are required")

    photo_path: Optional[str] = None
    if photo and photo.filename:
        photo_path = await _save_upload(photo, prefix="testimonial")

    t = models.Testimonial(
        name=name.strip(),
        location=location.strip() or None,
        quote=quote.strip(),
        photo_path=photo_path,
        sort_order=sort_order,
        is_active=is_active,
        created_by=admin.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    log_action(db, admin.id, "create_testimonial", "testimonial", t.id, name)
    return t


@router.put("/admin/pitham/testimonials/{tid}", response_model=schemas.TestimonialOut)
async def admin_update_testimonial(
    tid: int,
    name: Optional[str] = Form(None),
    quote: Optional[str] = Form(None),
    location: Optional[str] = Form(None),
    sort_order: Optional[int] = Form(None),
    is_active: Optional[bool] = Form(None),
    photo: Optional[UploadFile] = File(None),
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    t = db.query(models.Testimonial).filter(models.Testimonial.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Testimonial not found")

    if name is not None and name.strip():
        t.name = name.strip()
    if quote is not None and quote.strip():
        t.quote = quote.strip()
    if location is not None:
        t.location = location.strip() or None
    if sort_order is not None:
        t.sort_order = sort_order
    if is_active is not None:
        t.is_active = is_active
    if photo and photo.filename:
        new_path = await _save_upload(photo, prefix="testimonial")
        _delete_file(t.photo_path)
        t.photo_path = new_path

    db.commit()
    db.refresh(t)
    log_action(db, admin.id, "update_testimonial", "testimonial", t.id, t.name)
    return t


@router.delete("/admin/pitham/testimonials/{tid}")
def admin_delete_testimonial(
    tid: int,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    t = db.query(models.Testimonial).filter(models.Testimonial.id == tid).first()
    if not t:
        raise HTTPException(status_code=404, detail="Testimonial not found")
    _delete_file(t.photo_path)
    name = t.name
    db.delete(t)
    db.commit()
    log_action(db, admin.id, "delete_testimonial", "testimonial", tid, name)
    return {"message": "Deleted"}
