"""Authenticated file proxy that replaces the public /uploads StaticFiles mount.

Policy by path prefix:
    pitham/                    — public (banners, events, testimonials, gallery
                                  on the public Pitham page)
    broadcasts/                — any authenticated user (broadcast images are
                                  embedded in emails sent to many users anyway;
                                  enforcing list-membership at the file layer
                                  adds complexity without real privacy benefit)
    documents/, selfies/,
    analysis/, receipts/,
    invoices/, gallery/        — owner of the linked DB record OR any
                                  admin / moderator. Doc gallery (templates) is
                                  admin-only.

URLs are unchanged from the prior public mount, so no frontend code had to be
touched. Existing `fileUrl(path)` helpers keep working.
"""

import os
import re
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import get_db
import models
from utils.auth import COOKIE_NAME, decode_token

router = APIRouter(tags=["files"])

# Resolve the canonical absolute path of the uploads directory once at import
# time. We use this as the security floor for the path-traversal guard below —
# any resolved request path that doesn't sit underneath this prefix is rejected
# regardless of what the client passed.
UPLOADS_ROOT = os.path.abspath("uploads")
os.makedirs(UPLOADS_ROOT, exist_ok=True)

# Filenames like receipt_42.pdf / invoice_42.pdf encode the appointment id,
# which is what we use for ownership lookup since we don't persist the path
# on the appointment row for these.
_RECEIPT_RE = re.compile(r"^receipt_(\d+)\.pdf$", re.IGNORECASE)
_INVOICE_RE = re.compile(r"^invoice_(\d+)\.pdf$", re.IGNORECASE)


# ── Auth helpers ────────────────────────────────────────────────────────────

def _maybe_user(request: Request, db: Session) -> Optional[models.User]:
    """Resolve the calling user from cookie (preferred) or Bearer (legacy).

    Returns None on any failure — the caller decides whether to 401 or to
    treat as anonymous (public path). Never raises; corrupt tokens shouldn't
    be able to take down the file route."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        auth_header = request.headers.get("Authorization") or ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
    if not token:
        return None
    try:
        payload = decode_token(token)
        sub = payload.get("sub")
        if sub is None:
            return None
        return db.query(models.User).filter(models.User.id == int(sub)).first()
    except Exception:
        return None


def _is_staff(user: models.User) -> bool:
    """Admin OR moderator. We give all staff members read access to file
    contents — file-level section gating would be redundant given the API
    routes that surface these URLs are already section-gated."""
    return user.role in ("admin", "moderator")


# ── Ownership lookups — does this user own the linked record? ──────────────

def _user_owns_document(db: Session, user_id: int, db_path: str) -> bool:
    return (
        db.query(models.Document)
        .filter(models.Document.file_path == db_path, models.Document.user_id == user_id)
        .first()
        is not None
    )


def _user_owns_appt_file(db: Session, user_id: int, db_path: str) -> bool:
    """True if any appointment owned by `user_id` references this file via
    selfie / analysis / receipt path columns."""
    return (
        db.query(models.Appointment)
        .filter(
            models.Appointment.user_id == user_id,
            or_(
                models.Appointment.selfie_path == db_path,
                models.Appointment.analysis_path == db_path,
                models.Appointment.receipt_path == db_path,
            ),
        )
        .first()
        is not None
    )


def _user_owns_receipt_or_invoice(db: Session, user_id: int, filename: str) -> bool:
    """Receipts and invoices are stored as receipt_{id}.pdf / invoice_{id}.pdf
    where {id} is the appointment id. Verify ownership by parsing the id and
    looking up the appointment."""
    m = _RECEIPT_RE.match(filename) or _INVOICE_RE.match(filename)
    if not m:
        return False
    try:
        appt_id = int(m.group(1))
    except ValueError:
        return False
    appt = (
        db.query(models.Appointment)
        .filter(models.Appointment.id == appt_id, models.Appointment.user_id == user_id)
        .first()
    )
    return appt is not None


# ── The route ──────────────────────────────────────────────────────────────

def _resolve(path: str) -> str:
    """Validate `path` and return the absolute filesystem path under uploads.
    Blocks `..`, absolute paths, and any traversal that escapes UPLOADS_ROOT."""
    # Reject early on suspicious shapes so we don't even normpath them
    if not path or "\x00" in path:
        raise HTTPException(status_code=404, detail="Not found")
    requested = os.path.abspath(os.path.join(UPLOADS_ROOT, path))
    # Must be inside the uploads root after normalisation
    if not (requested == UPLOADS_ROOT or requested.startswith(UPLOADS_ROOT + os.sep)):
        raise HTTPException(status_code=404, detail="Not found")
    if not os.path.isfile(requested):
        raise HTTPException(status_code=404, detail="Not found")
    return requested


def _serve(path: str, *, public: bool) -> FileResponse:
    response = FileResponse(path)
    if public:
        # Public CMS assets: aggressive shared-cache. Filenames are uuid-suffixed
        # so changing a banner publishes a new URL — safe to cache forever.
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    else:
        # Personal files: per-browser cache only, short TTL so revoking a doc
        # propagates within minutes even if a stale URL lingers.
        response.headers["Cache-Control"] = "private, max-age=300, must-revalidate"
    return response


@router.get("/uploads/{path:path}")
def serve_upload(path: str, request: Request, db: Session = Depends(get_db)):
    requested = _resolve(path)

    # Normalise the relative path back to forward slashes for prefix checks +
    # DB lookups (paths are stored that way in the DB regardless of OS).
    rel = path.replace("\\", "/").lstrip("/")
    db_path = "uploads/" + rel

    # ── Public bucket: no auth required ────────────────────────────────────
    if rel.startswith("pitham/"):
        return _serve(requested, public=True)

    # ── Everything below requires authentication ──────────────────────────
    user = _maybe_user(request, db)
    if not user:
        raise HTTPException(status_code=401, detail="Authentication required")

    # Staff (admin/moderator) bypass — needed for review screens that show
    # any user's selfie / analysis / receipts.
    if _is_staff(user):
        return _serve(requested, public=False)

    # Broadcast images: any authenticated user. Recipients vary by list, but
    # the image is also rendered inside the broadcast email body, so there's
    # no extra leak from allowing other authenticated users to read it.
    if rel.startswith("broadcasts/"):
        return _serve(requested, public=False)

    # Gallery (admin doc templates): admin/moderator only — already blocked
    # by the staff bypass above. Anonymous users can't reach here.
    if rel.startswith("gallery/"):
        raise HTTPException(status_code=403, detail="Forbidden")

    # Personal files: must own the linked record.
    if rel.startswith("documents/") and _user_owns_document(db, user.id, db_path):
        return _serve(requested, public=False)

    if (
        rel.startswith("selfies/")
        or rel.startswith("analysis/")
    ) and _user_owns_appt_file(db, user.id, db_path):
        return _serve(requested, public=False)

    if (rel.startswith("receipts/") or rel.startswith("invoices/")) and _user_owns_receipt_or_invoice(
        db, user.id, os.path.basename(rel)
    ):
        return _serve(requested, public=False)

    # Unknown bucket OR not the owner — 403, not 404, so the user knows the
    # file exists but they can't see it. (Bucket existence is not sensitive.)
    raise HTTPException(status_code=403, detail="Forbidden")
