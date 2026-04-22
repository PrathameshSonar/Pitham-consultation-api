"""
Admin-configurable site settings.
Keys:
  consultation_fee       — amount in rupees (e.g. "500")
  booking_enabled        — "true" or "false"
  booking_resume_date    — ISO date when bookings reopen (e.g. "2026-05-01")
  booking_hold_message   — custom message shown when bookings are paused
  booking_limit          — max bookings allowed ("0" = unlimited)
  booking_limit_deadline — ISO datetime cutoff for bookings (e.g. "2026-04-20T18:00")
  consultation_terms     — HTML content for consultation T&C shown before booking
"""

import bleach
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from database import get_db
import models
from utils.auth import require_admin, require_super_admin, get_current_user
from utils.audit import log_action
from datetime import datetime

# Allowed HTML tags/attrs for consultation terms (rich text editor output)
ALLOWED_TAGS = [
    "h1", "h2", "h3", "h4", "h5", "h6",
    "p", "br", "strong", "b", "em", "i", "u", "s", "strike",
    "ol", "ul", "li", "a", "span", "blockquote",
]
ALLOWED_ATTRS = {
    "a": ["href", "target", "rel"],
    "span": ["style"],
}

router = APIRouter(tags=["settings"])

DEFAULT_TERMS = """<h3>Consultation Terms &amp; Conditions</h3>
<ol>
<li><strong>Services:</strong> Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar (SPBSP) provides astrology and spiritual consultation by Shri Mayuresh Guruji Vispute via Zoom.</li>
<li><strong>Payment:</strong> Full payment is required before scheduling. Payments are non-refundable once the session is confirmed.</li>
<li><strong>Privacy:</strong> Your personal information and consultation records are kept strictly confidential.</li>
<li><strong>Rescheduling:</strong> If Guruji needs to reschedule, it will be done at no extra cost. For user-initiated rescheduling, raise a query.</li>
<li><strong>Disclaimer:</strong> Astrological guidance is for spiritual and informational purposes only. It does not constitute medical, legal, or financial advice.</li>
<li><strong>Conduct:</strong> Users must be respectful during consultations. Inappropriate behaviour may lead to session cancellation without refund.</li>
</ol>"""

DEFAULTS = {
    "consultation_fee": "3500",
    "booking_enabled": "true",
    "booking_resume_date": "",
    "booking_hold_message": "",
    "booking_limit": "0",
    "booking_limit_deadline": "",
    "consultation_terms": DEFAULT_TERMS,
    # Social links
    "social_facebook": "",
    "social_instagram": "",
    "social_youtube": "",
    "social_twitter": "",
    "social_whatsapp": "",
    # Contact info
    "contact_email": "",
    "contact_phone": "",
    "contact_address": "",
    "contact_map_url": "",
}


def _get(db: Session, key: str) -> str:
    row = db.query(models.SiteSetting).filter(models.SiteSetting.key == key).first()
    return row.value if row else DEFAULTS.get(key, "")


def _set(db: Session, key: str, value: str):
    row = db.query(models.SiteSetting).filter(models.SiteSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(models.SiteSetting(key=key, value=value))
    db.commit()


# ── Public: read settings (needed by frontend) ─────────────────────────────

@router.get("/settings/public")
def get_public_settings(db: Session = Depends(get_db)):
    return {
        "consultation_fee": int(_get(db, "consultation_fee") or "500"),
        "booking_enabled": _get(db, "booking_enabled") == "true",
        "booking_resume_date": _get(db, "booking_resume_date"),
        "booking_hold_message": _get(db, "booking_hold_message"),
        "booking_limit": int(_get(db, "booking_limit") or "0"),
        "booking_limit_deadline": _get(db, "booking_limit_deadline"),
        "consultation_terms": _get(db, "consultation_terms"),
        "social_facebook": _get(db, "social_facebook"),
        "social_instagram": _get(db, "social_instagram"),
        "social_youtube": _get(db, "social_youtube"),
        "social_twitter": _get(db, "social_twitter"),
        "social_whatsapp": _get(db, "social_whatsapp"),
        "contact_email": _get(db, "contact_email"),
        "contact_phone": _get(db, "contact_phone"),
        "contact_address": _get(db, "contact_address"),
        "contact_map_url": _get(db, "contact_map_url"),
    }


# ── Admin: read/write all settings ─────────────────────────────────────────

class UpdateSettingsRequest(BaseModel):
    consultation_fee: Optional[int] = None
    booking_enabled: Optional[bool] = None
    booking_resume_date: Optional[str] = None
    booking_hold_message: Optional[str] = None
    booking_limit: Optional[int] = None
    booking_limit_deadline: Optional[str] = None
    consultation_terms: Optional[str] = None
    # Contact & Social
    contact_email: Optional[str] = None
    contact_phone: Optional[str] = None
    contact_address: Optional[str] = None
    contact_map_url: Optional[str] = None
    social_facebook: Optional[str] = None
    social_instagram: Optional[str] = None
    social_youtube: Optional[str] = None
    social_twitter: Optional[str] = None
    social_whatsapp: Optional[str] = None


@router.get("/admin/settings")
def admin_get_settings(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    return {k: _get(db, k) for k in DEFAULTS}


@router.put("/admin/settings")
def admin_update_settings(
    data: UpdateSettingsRequest,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    # Moderators: booking/limits/deadline save directly; fee + T&C go to approval
    if admin.role == "moderator":
        # Direct save: booking toggle, limits, deadline, hold message, resume date
        if data.booking_enabled is not None:
            _set(db, "booking_enabled", "true" if data.booking_enabled else "false")
        if data.booking_resume_date is not None:
            _set(db, "booking_resume_date", data.booking_resume_date)
        if data.booking_hold_message is not None:
            _set(db, "booking_hold_message", data.booking_hold_message)
        if data.booking_limit is not None:
            _set(db, "booking_limit", str(data.booking_limit))
        if data.booking_limit_deadline is not None:
            _set(db, "booking_limit_deadline", data.booking_limit_deadline)
        # Contact & Social — moderator can also save
        for key in ["contact_email", "contact_phone", "contact_address",
                    "social_facebook", "social_instagram", "social_youtube",
                    "social_twitter", "social_whatsapp"]:
            val = getattr(data, key, None)
            if val is not None:
                _set(db, key, val)

        # Pending approval: fee + T&C
        pending = {}
        if data.consultation_fee is not None:
            pending["consultation_fee"] = str(data.consultation_fee)
        if data.consultation_terms is not None:
            pending["consultation_terms"] = bleach.clean(
                data.consultation_terms, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True
            )
        if pending:
            for key, value in pending.items():
                db.add(models.PendingSettingChange(
                    submitted_by=admin.id, key=key, value=value,
                ))
            db.commit()
            log_action(db, admin.id, "submit_settings_approval", "settings", 0,
                       f"Pending: {', '.join(pending.keys())}")
            return {"message": "Booking settings saved. Fee/T&C submitted for super admin approval.", "pending": True}

        db.commit()
        log_action(db, admin.id, "update_settings", "settings", 0, "Booking settings updated")
        return {"message": "Settings updated"}

    # Super admin — apply directly
    if data.consultation_fee is not None:
        _set(db, "consultation_fee", str(data.consultation_fee))
    if data.booking_enabled is not None:
        _set(db, "booking_enabled", "true" if data.booking_enabled else "false")
    if data.booking_resume_date is not None:
        _set(db, "booking_resume_date", data.booking_resume_date)
    if data.booking_hold_message is not None:
        _set(db, "booking_hold_message", data.booking_hold_message)
    if data.booking_limit is not None:
        _set(db, "booking_limit", str(data.booking_limit))
    if data.booking_limit_deadline is not None:
        _set(db, "booking_limit_deadline", data.booking_limit_deadline)
    if data.consultation_terms is not None:
        sanitized = bleach.clean(data.consultation_terms, tags=ALLOWED_TAGS, attributes=ALLOWED_ATTRS, strip=True)
        _set(db, "consultation_terms", sanitized)
    # Contact & Social — both roles can save directly
    for key in ["contact_email", "contact_phone", "contact_address", "contact_map_url",
                "social_facebook", "social_instagram", "social_youtube",
                "social_twitter", "social_whatsapp"]:
        val = getattr(data, key, None)
        if val is not None:
            _set(db, key, val)
    log_action(db, admin.id, "update_settings", "settings", 0, "Settings updated")
    return {"message": "Settings updated"}


# ── Pending settings approval (super admin only) ─────────────────────────────

@router.get("/admin/settings/pending")
def get_pending_changes(
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    pending = (
        db.query(models.PendingSettingChange)
        .filter(models.PendingSettingChange.status == "pending")
        .order_by(models.PendingSettingChange.created_at.desc())
        .all()
    )
    admin_ids = {p.submitted_by for p in pending}
    names = {u.id: u.name for u in db.query(models.User).filter(models.User.id.in_(admin_ids)).all()} if admin_ids else {}
    return [
        {
            "id": p.id,
            "submitted_by": names.get(p.submitted_by, f"Admin #{p.submitted_by}"),
            "key": p.key,
            "value": p.value,
            "created_at": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pending
    ]


@router.post("/admin/settings/pending/{change_id}/approve")
def approve_change(
    change_id: int,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    change = db.query(models.PendingSettingChange).filter(
        models.PendingSettingChange.id == change_id,
        models.PendingSettingChange.status == "pending",
    ).first()
    if not change:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Pending change not found")
    _set(db, change.key, change.value)
    change.status = "approved"
    change.reviewed_by = admin.id
    change.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, admin.id, "approve_setting", "settings", change.id, f"Key: {change.key}")
    return {"message": f"Setting '{change.key}' approved and applied."}


@router.post("/admin/settings/pending/{change_id}/reject")
def reject_change(
    change_id: int,
    admin: models.User = Depends(require_super_admin),
    db: Session = Depends(get_db),
):
    change = db.query(models.PendingSettingChange).filter(
        models.PendingSettingChange.id == change_id,
        models.PendingSettingChange.status == "pending",
    ).first()
    if not change:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Pending change not found")
    change.status = "rejected"
    change.reviewed_by = admin.id
    change.reviewed_at = datetime.utcnow()
    db.commit()
    log_action(db, admin.id, "reject_setting", "settings", change.id, f"Key: {change.key}")
    return {"message": f"Setting '{change.key}' rejected."}
