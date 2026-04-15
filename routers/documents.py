import os
import re
import shutil
import aiofiles
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from typing import List, Optional
import json
import uuid

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20MB


async def _validate_and_read(file: UploadFile, max_size: int = MAX_FILE_SIZE) -> bytes:
    """Read file content with size validation."""
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail=f"File must be under {max_size // (1024*1024)}MB.")
    return content


def _safe_filename(name: str) -> str:
    """Sanitize filename to prevent path traversal."""
    return re.sub(r"[^\w.\-]", "_", os.path.basename(name))

from database import get_db
import models
import schemas
from utils.auth import get_current_user, require_admin
from utils.audit import log_action

router = APIRouter(tags=["documents"])

ASSIGNED_DIR = "uploads/documents"
GALLERY_DIR  = "uploads/gallery"
os.makedirs(ASSIGNED_DIR, exist_ok=True)
os.makedirs(GALLERY_DIR, exist_ok=True)


# ── Admin: Gallery (reusable templates) ──────────────────────────────────────

@router.post("/admin/documents/gallery", response_model=schemas.DocumentOut)
async def upload_gallery_document(
    title: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    content = await _validate_and_read(file)
    filename = f"template_{_safe_filename(file.filename or 'doc')}"
    file_path = os.path.join(GALLERY_DIR, filename)
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    doc = models.Document(
        user_id=None,
        title=title,
        description=description,
        file_path=file_path,
        uploaded_by=admin.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    log_action(db, admin.id, "upload_gallery_doc", "document", doc.id, f"Title: {title}")
    return doc


@router.get("/admin/documents/gallery", response_model=List[schemas.DocumentOut])
def list_gallery_documents(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Document)
        .filter(models.Document.user_id.is_(None))
        .order_by(models.Document.created_at.desc())
        .all()
    )


@router.delete("/admin/documents/gallery/{doc_id}")
def delete_gallery_document(
    doc_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.user_id.is_(None))
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Gallery document not found")

    if doc.file_path and os.path.exists(doc.file_path):
        try: os.remove(doc.file_path)
        except OSError: pass

    title = doc.title
    db.delete(doc)
    db.commit()
    log_action(db, admin.id, "delete_gallery_doc", "document", doc_id, f"Title: {title}")
    return {"message": "Deleted"}


# ── Admin: Assign from gallery to a user (copies the file) ───────────────────

@router.post("/admin/documents/assign-from-gallery", response_model=schemas.DocumentOut)
def assign_from_gallery(
    data: schemas.AssignFromGalleryRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    template = (
        db.query(models.Document)
        .filter(models.Document.id == data.gallery_doc_id,
                models.Document.user_id.is_(None))
        .first()
    )
    if not template:
        raise HTTPException(status_code=404, detail="Gallery document not found")

    target_user = db.query(models.User).filter(models.User.id == data.user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Copy the file so deletion of the gallery template won't affect the user's doc.
    orig_name = os.path.basename(template.file_path)
    new_name  = f"{data.user_id}_{template.id}_{orig_name}"
    new_path  = os.path.join(ASSIGNED_DIR, new_name)
    try:
        shutil.copyfile(template.file_path, new_path)
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not copy file: {e}")

    doc = models.Document(
        user_id=data.user_id,
        title=template.title,
        description=template.description,
        file_path=new_path,
        uploaded_by=admin.id,
        source_template_id=template.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


# ── Admin: Bulk assign an existing gallery template to multiple users ───────

@router.post("/admin/documents/bulk-assign-gallery", response_model=schemas.BulkAssignResponse)
def bulk_assign_from_gallery(
    data: schemas.BulkAssignRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not data.gallery_doc_id:
        raise HTTPException(status_code=400, detail="gallery_doc_id is required")
    if not data.user_ids:
        raise HTTPException(status_code=400, detail="user_ids is required")

    template = (
        db.query(models.Document)
        .filter(models.Document.id == data.gallery_doc_id,
                models.Document.user_id.is_(None))
        .first()
    )
    if not template:
        raise HTTPException(status_code=404, detail="Gallery document not found")

    assigned = 0
    skipped: list[str] = []
    batch_id    = str(uuid.uuid4())
    batch_label = data.batch_label or f"Bulk: {len(data.user_ids)} users"

    for uid in set(data.user_ids):
        user = db.query(models.User).filter(models.User.id == uid).first()
        if not user:
            skipped.append(f"User #{uid}: not found")
            continue
        try:
            orig_name = os.path.basename(template.file_path)
            new_name  = f"{uid}_{template.id}_{orig_name}"
            new_path  = os.path.join(ASSIGNED_DIR, new_name)
            shutil.copyfile(template.file_path, new_path)

            doc = models.Document(
                user_id=uid,
                title=template.title,
                description=template.description,
                file_path=new_path,
                uploaded_by=admin.id,
                source_template_id=template.id,
                batch_id=batch_id,
                batch_label=batch_label,
            )
            db.add(doc)
            assigned += 1
        except Exception as e:
            skipped.append(f"{user.name}: {e}")

    db.commit()
    log_action(db, admin.id, "bulk_assign_doc", "document", data.gallery_doc_id or 0,
               f"Assigned to {assigned} users")
    return {"assigned_count": assigned, "skipped": skipped}


# ── Admin: Bulk upload a new file for multiple users ────────────────────────

@router.post("/admin/documents/bulk-upload", response_model=schemas.BulkAssignResponse)
async def bulk_upload_for_users(
    user_ids: str = Form(...),          # JSON array: "[1,2,3]"
    title: str = Form(...),
    description: str = Form(""),
    batch_label: str = Form(""),
    file: UploadFile = File(...),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        ids = json.loads(user_ids)
        if not isinstance(ids, list) or not all(isinstance(i, int) for i in ids):
            raise ValueError
    except Exception:
        raise HTTPException(status_code=400, detail="user_ids must be a JSON array of ints")

    if not ids:
        raise HTTPException(status_code=400, detail="No users selected")

    # Save the source file once to the gallery dir (as a staging file),
    # then copy to each user's document path.
    content = await _validate_and_read(file)
    source_name = f"bulk_{admin.id}_{_safe_filename(file.filename or 'doc')}"
    source_path = os.path.join(GALLERY_DIR, source_name)
    async with aiofiles.open(source_path, "wb") as f:
        await f.write(content)

    assigned = 0
    skipped: list[str] = []
    batch_id    = str(uuid.uuid4())
    effective_label = batch_label or f"Bulk upload: {len(ids)} users"

    for uid in set(ids):
        user = db.query(models.User).filter(models.User.id == uid).first()
        if not user:
            skipped.append(f"User #{uid}: not found")
            continue
        try:
            new_name = f"{uid}_{file.filename}"
            new_path = os.path.join(ASSIGNED_DIR, new_name)
            shutil.copyfile(source_path, new_path)

            doc = models.Document(
                user_id=uid,
                title=title,
                description=description,
                file_path=new_path,
                uploaded_by=admin.id,
                batch_id=batch_id,
                batch_label=effective_label,
            )
            db.add(doc)
            assigned += 1
        except Exception as e:
            skipped.append(f"{user.name}: {e}")

    # Clean up the staging source file.
    try: os.remove(source_path)
    except OSError: pass

    db.commit()
    log_action(db, admin.id, "bulk_upload_doc", "document", 0,
               f"'{title}' to {assigned} users")
    return {"assigned_count": assigned, "skipped": skipped}


# ── Admin: Upload directly for a user (keeps existing behaviour) ─────────────

@router.post("/admin/documents", response_model=schemas.DocumentOut)
async def upload_document(
    user_id: int = Form(...),
    title: str = Form(...),
    description: str = Form(""),
    file: UploadFile = File(...),
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    target_user = db.query(models.User).filter(models.User.id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    content = await _validate_and_read(file)
    filename = f"{user_id}_{_safe_filename(file.filename or 'doc')}"
    file_path = os.path.join(ASSIGNED_DIR, filename)
    async with aiofiles.open(file_path, "wb") as f:
        await f.write(content)

    doc = models.Document(
        user_id=user_id,
        title=title,
        description=description,
        file_path=file_path,
        uploaded_by=admin.id,
    )
    db.add(doc)
    db.commit()
    db.refresh(doc)
    return doc


# ── Admin: list assigned documents (not gallery) ─────────────────────────────

@router.get("/admin/documents", response_model=List[schemas.DocumentOut])
def admin_list_documents(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Document)
        .filter(models.Document.user_id.is_not(None))
        .order_by(models.Document.created_at.desc())
        .all()
    )


# ── Admin: delete an assigned document ──────────────────────────────────────

@router.delete("/admin/documents/{doc_id}")
def delete_assigned_document(
    doc_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    doc = (
        db.query(models.Document)
        .filter(models.Document.id == doc_id, models.Document.user_id.is_not(None))
        .first()
    )
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if doc.file_path and os.path.exists(doc.file_path):
        try: os.remove(doc.file_path)
        except OSError: pass

    title = doc.title
    user_id = doc.user_id
    db.delete(doc)
    db.commit()
    log_action(db, admin.id, "delete_assigned_doc", "document", doc_id,
               f"'{title}' from user #{user_id}")
    return {"message": "Document removed from user"}


# ── User: my documents ────────────────────────────────────────────────────────

@router.get("/documents/my", response_model=List[schemas.DocumentOut])
def my_documents(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Document)
        .filter(models.Document.user_id == user.id)
        .order_by(models.Document.created_at.desc())
        .all()
    )
