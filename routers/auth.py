import logging
import os
import re
import secrets
import bleach
from datetime import datetime, timedelta

logger = logging.getLogger("pitham.auth")
from pydantic import BaseModel
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

from database import get_db
import models
import schemas
from utils.auth import (
    hash_password, verify_password, create_token, get_current_user,
    set_auth_cookie, clear_auth_cookie,
)
from utils.email import send_email, send_password_reset_otp

router = APIRouter(prefix="/auth", tags=["auth"])
limiter = Limiter(key_func=get_remote_address)

from config import settings
GOOGLE_CLIENT_ID = settings.google.client_id


# ── Pydantic models ─────────────────────────────────────────────────────────

class ProfileUpdateRequest(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    mobile: Optional[str] = None
    dob: Optional[str] = None
    tob: Optional[str] = None
    birth_place: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    country: Optional[str] = None


class ForgotPasswordRequest(BaseModel):
    email: Optional[str] = None
    mobile: Optional[str] = None


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


# ── Helpers ──────────────────────────────────────────────────────────────────

def _sanitize(val: str) -> str:
    """Strip HTML tags and trim whitespace."""
    return bleach.clean(val.strip(), tags=[], strip=True) if val else ""


def _validate_mobile(mobile: str) -> str:
    """Allow digits, +, spaces, dashes only."""
    cleaned = re.sub(r"[^\d+\-\s]", "", mobile.strip())
    if cleaned and len(re.sub(r"\D", "", cleaned)) < 7:
        raise HTTPException(status_code=400, detail="Invalid mobile number")
    return cleaned


# ── Register ─────────────────────────────────────────────────────────────────

@router.post("/register", response_model=schemas.TokenResponse)
@limiter.limit("10/minute")
def register(request: Request, response: Response, data: schemas.RegisterRequest, db: Session = Depends(get_db)):
    name = _sanitize(data.name)
    email = _sanitize(data.email) if data.email else ""
    mobile = _validate_mobile(data.mobile) if data.mobile else ""

    if not email and not mobile:
        raise HTTPException(status_code=400, detail="Email or mobile is required")
    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Name is too short")
    if len(data.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    if email:
        if db.query(models.User).filter(models.User.email == email).first():
            raise HTTPException(status_code=400, detail="Email already registered")

    if mobile:
        if db.query(models.User).filter(models.User.mobile == mobile).first():
            raise HTTPException(status_code=400, detail="Mobile number already registered")

    user = models.User(
        name=name,
        email=email or None,
        mobile=mobile,
        dob=data.dob,
        tob=data.tob,
        birth_place=data.birth_place,
        city=data.city,
        state=data.state,
        country=data.country,
        hashed_password=hash_password(data.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    # Best-effort: send verification email immediately on signup. Don't fail
    # registration if mailer is misconfigured — user can request resend later.
    if user.email:
        try:
            _send_verification_for(user, db)
        except Exception as e:
            logger.warning("Auto verification email failed for user_id=%s: %s", user.id, e)

    token = create_token({"sub": str(user.id), "role": user.role})
    set_auth_cookie(response, token)
    from utils.permissions import get_user_permissions
    return {"token": token, "role": user.role, "name": user.name, "permissions": get_user_permissions(user)}


# ── Login ────────────────────────────────────────────────────────────────────

@router.post("/login", response_model=schemas.TokenResponse)
@limiter.limit("15/minute")
def login(request: Request, response: Response, data: schemas.LoginRequest, db: Session = Depends(get_db)):
    identifier = data.email or data.mobile
    if not identifier:
        raise HTTPException(status_code=400, detail="Email or mobile is required")

    user = None
    if data.email:
        user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user and data.mobile:
        user = db.query(models.User).filter(models.User.mobile == data.mobile).first()
    if not user and data.email and "@" not in data.email:
        user = db.query(models.User).filter(models.User.mobile == data.email).first()

    if not user or not user.hashed_password or not verify_password(data.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    token = create_token({"sub": str(user.id), "role": user.role})
    set_auth_cookie(response, token)
    from utils.permissions import get_user_permissions
    return {"token": token, "role": user.role, "name": user.name, "permissions": get_user_permissions(user)}


# ── Google Login ─────────────────────────────────────────────────────────────

@router.post("/google", response_model=schemas.TokenResponse)
@limiter.limit("15/minute")
def google_login(request: Request, response: Response, data: schemas.GoogleLoginRequest, db: Session = Depends(get_db)):
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=500, detail="Google login not configured")

    try:
        idinfo = id_token.verify_oauth2_token(
            data.credential,
            google_requests.Request(),
            GOOGLE_CLIENT_ID,
        )
    except ValueError:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    google_id = idinfo["sub"]
    email = idinfo.get("email", "")
    name = idinfo.get("name", "")

    user = db.query(models.User).filter(models.User.google_id == google_id).first()
    if not user and email:
        user = db.query(models.User).filter(models.User.email == email).first()
        if user:
            user.google_id = google_id
            db.commit()

    if not user:
        user = models.User(
            name=name,
            email=email or None,
            mobile="",
            dob="",
            tob="",
            birth_place="",
            city="",
            state="",
            country="India",
            hashed_password=None,
            google_id=google_id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)

    token = create_token({"sub": str(user.id), "role": user.role})
    set_auth_cookie(response, token)
    from utils.permissions import get_user_permissions
    return {"token": token, "role": user.role, "name": user.name, "permissions": get_user_permissions(user)}


# ── Logout — clears the auth cookie (Bearer clients can just drop their token) ─

@router.post("/logout")
def logout(response: Response):
    clear_auth_cookie(response)
    return {"message": "Logged out"}


# ── Profile ──────────────────────────────────────────────────────────────────

@router.get("/profile", response_model=schemas.UserOut)
def get_profile(user: models.User = Depends(get_current_user)):
    return user


@router.put("/profile", response_model=schemas.UserOut)
def update_profile(
    data: ProfileUpdateRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.name is not None:
        user.name = _sanitize(data.name)
    if data.email is not None:
        clean_email = _sanitize(data.email)
        if clean_email and clean_email != user.email:
            existing = db.query(models.User).filter(
                models.User.email == clean_email, models.User.id != user.id
            ).first()
            if existing:
                raise HTTPException(status_code=400, detail="Email already in use")
            user.email = clean_email
    if data.mobile is not None:
        clean_mobile = _validate_mobile(data.mobile)
        if clean_mobile and clean_mobile != user.mobile:
            existing = db.query(models.User).filter(
                models.User.mobile == clean_mobile, models.User.id != user.id
            ).first()
            if existing:
                raise HTTPException(status_code=400, detail="Mobile already in use")
            user.mobile = clean_mobile
    if data.dob is not None:
        user.dob = _sanitize(data.dob)
    if data.tob is not None:
        user.tob = _sanitize(data.tob)
    if data.birth_place is not None:
        user.birth_place = _sanitize(data.birth_place)
    if data.city is not None:
        user.city = _sanitize(data.city)
    if data.state is not None:
        user.state = _sanitize(data.state)
    if data.country is not None:
        user.country = _sanitize(data.country)
    db.commit()
    db.refresh(user)
    return user


# ── Account Deletion (DPDP / GDPR right-to-erasure) ─────────────────────────

@router.delete("/account")
def delete_account(
    response: Response,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """User-initiated account deletion. Anonymizes appointments (preserves accounting
    records as required for tax / financial compliance) and removes everything else
    that's personal."""
    if user.role == "admin":
        # Last admin lockout protection. Demote first via super-admin route, then delete.
        raise HTTPException(status_code=400, detail="Admins cannot self-delete. Demote first.")

    user_id = user.id

    # Detach personal data from appointments but keep the row for revenue records.
    appts = db.query(models.Appointment).filter(models.Appointment.user_id == user_id).all()
    for a in appts:
        a.name = "(deleted user)"
        a.email = ""
        a.mobile = ""
        a.dob = ""
        a.tob = ""
        a.birth_place = ""
        a.problem = ""
        a.selfie_path = None
        a.notes = None

    # Delete user-owned content with no accounting value
    db.query(models.Document).filter(models.Document.user_id == user_id).delete()
    db.query(models.Query).filter(models.Query.user_id == user_id).delete()
    db.query(models.Recording).filter(models.Recording.user_id == user_id).delete()
    db.query(models.UserListMember).filter(models.UserListMember.user_id == user_id).delete()
    # Remove pending one-time tokens that referenced this user
    rows = db.query(models.SiteSetting).filter(
        models.SiteSetting.key.like("reset:%") | models.SiteSetting.key.like("verify:%")
    ).all()
    for r in rows:
        if (r.value or "").startswith(f"{user_id}:"):
            db.delete(r)

    db.delete(user)
    db.commit()

    clear_auth_cookie(response)
    logger.info("Account deleted: user_id=%s", user_id)
    return {"message": "Account deleted"}


# ── DPDP: download my data ───────────────────────────────────────────────────

@router.get("/account/export")
def export_my_data(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns a JSON dump of everything personal we hold for this user.
    Required by DPDP / GDPR-style "right to access". Excludes hashed_password
    and any internal-only fields, includes appointments, queries, documents,
    recordings, list memberships, notification prefs."""
    appts = db.query(models.Appointment).filter(models.Appointment.user_id == user.id).all()
    docs = db.query(models.Document).filter(models.Document.user_id == user.id).all()
    queries = db.query(models.Query).filter(models.Query.user_id == user.id).all()
    recordings = db.query(models.Recording).filter(models.Recording.user_id == user.id).all()
    list_memberships = (
        db.query(models.UserListMember).filter(models.UserListMember.user_id == user.id).all()
    )

    def _iso(dt):
        return dt.isoformat() if dt else None

    return {
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "profile": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "mobile": user.mobile,
            "dob": user.dob,
            "tob": user.tob,
            "birth_place": user.birth_place,
            "city": user.city,
            "state": user.state,
            "country": user.country,
            "role": user.role,
            "email_verified": user.email_verified,
            "notify_email": user.notify_email,
            "notify_sms": user.notify_sms,
            "created_at": _iso(user.created_at),
        },
        "appointments": [
            {
                "id": a.id,
                "name": a.name,
                "email": a.email,
                "mobile": a.mobile,
                "dob": a.dob,
                "tob": a.tob,
                "birth_place": a.birth_place,
                "problem": a.problem,
                "status": a.status,
                "payment_status": a.payment_status,
                "payment_reference": a.payment_reference,
                "scheduled_date": a.scheduled_date,
                "scheduled_time": a.scheduled_time,
                "zoom_link": a.zoom_link,
                "agreed_terms": a.agreed_terms,
                "created_at": _iso(a.created_at),
            }
            for a in appts
        ],
        "documents": [
            {"id": d.id, "title": d.title, "description": d.description, "file_path": d.file_path, "created_at": _iso(d.created_at)}
            for d in docs
        ],
        "queries": [
            {"id": q.id, "subject": q.subject, "message": q.message, "reply": q.reply, "status": q.status, "created_at": _iso(q.created_at)}
            for q in queries
        ],
        "recordings": [
            {"id": r.id, "title": r.title, "zoom_recording_url": r.zoom_recording_url, "appointment_id": r.appointment_id, "created_at": _iso(r.created_at)}
            for r in recordings
        ],
        "list_memberships": [
            {"user_list_id": m.user_list_id} for m in list_memberships
        ],
    }


# ── Notification Preferences ──────────────────────────────────────────────────

class NotificationPrefsRequest(BaseModel):
    notify_email: Optional[bool] = None
    notify_sms: Optional[bool] = None


@router.put("/profile/notifications")
def update_notification_prefs(
    data: NotificationPrefsRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if data.notify_email is not None:
        user.notify_email = data.notify_email
    if data.notify_sms is not None:
        user.notify_sms = data.notify_sms
    db.commit()
    return {"message": "Notification preferences updated"}


# ── Email Verification ───────────────────────────────────────────────────────


def _send_verification_for(user: models.User, db: Session) -> None:
    """Mint a one-time verification token, persist it, and email the link.
    Raises if the email send fails — caller decides whether to swallow."""
    token = secrets.token_urlsafe(32)
    expiry = (datetime.utcnow() + timedelta(hours=24)).isoformat()
    db.merge(models.SiteSetting(key=f"verify:{token}", value=f"{user.id}:{expiry}"))
    db.commit()

    verify_link = f"{settings.core.frontend_url}/verify-email?token={token}"

    send_email(
        to=user.email,
        subject="Verify Your Email — SPBSP, Ahilyanagar",
        html_body=f"""
        <div style="font-family:'Poppins',sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
          <div style="text-align:center;margin-bottom:20px">
            <h2 style="color:#7B1E1E">Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar</h2>
          </div>
          <h3>Namaste {user.name},</h3>
          <p>Click the link below to verify your email address:</p>
          <p style="text-align:center;margin:24px 0">
            <a href="{verify_link}" style="background:#E65100;color:#fff;padding:12px 32px;border-radius:999px;text-decoration:none;font-weight:600">
              Verify Email
            </a>
          </p>
          <p style="color:#9C8573;font-size:12px">This link expires in 24 hours.</p>
        </div>
        """,
    )


@router.post("/send-verification")
@limiter.limit("3/minute")
def send_verification_email(request: Request, user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    if user.email_verified:
        return {"message": "Email already verified"}
    if not user.email:
        raise HTTPException(status_code=400, detail="No email on account")

    _send_verification_for(user, db)
    return {"message": "Verification email sent"}


@router.get("/verify-email")
def verify_email(token: str = Query(...), db: Session = Depends(get_db)):
    row = db.query(models.SiteSetting).filter(models.SiteSetting.key == f"verify:{token}").first()
    if not row:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")

    parts = row.value.split(":", 1)
    if len(parts) != 2:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid token")

    user_id, expiry_str = parts
    try:
        if datetime.utcnow() > datetime.fromisoformat(expiry_str):
            db.delete(row)
            db.commit()
            from fastapi import HTTPException
            raise HTTPException(status_code=400, detail="Verification link expired")
    except ValueError:
        pass

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if user:
        user.email_verified = True
        db.delete(row)
        db.commit()
    return {"message": "Email verified successfully"}


# ── Forgot / Reset Password ─────────────────────────────────────────────────

@router.post("/forgot-password")
@limiter.limit("5/minute")
def forgot_password(request: Request, data: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """
    Generate a reset token. In production, send it via email/SMS.
    For now, return it in the response (for testing).
    """
    if not data.email and not data.mobile:
        raise HTTPException(status_code=400, detail="Email or mobile is required")

    user = None
    if data.email:
        user = db.query(models.User).filter(models.User.email == data.email).first()
    if not user and data.mobile:
        user = db.query(models.User).filter(models.User.mobile == data.mobile).first()

    if not user:
        # Don't reveal whether the user exists
        return {"message": "If an account exists, a reset link has been sent."}

    # Generate a 6-digit numeric OTP and store it (valid for 10 minutes)
    reset_token = f"{secrets.randbelow(1_000_000):06d}"
    expiry = (datetime.utcnow() + timedelta(minutes=10)).isoformat()
    db.merge(models.SiteSetting(key=f"reset:{reset_token}", value=f"{user.id}:{expiry}"))
    db.commit()

    # Send OTP via email AND WhatsApp (each silently no-ops if not configured / no recipient)
    send_password_reset_otp(
        to_email=user.email or "",
        to_mobile=user.mobile or "",
        name=user.name,
        otp=reset_token,
    )

    logger.info("Password reset requested for user %s", user.id)

    return {"message": "If an account exists, a reset link has been sent."}


@router.post("/reset-password")
@limiter.limit("5/minute")
def reset_password(request: Request, data: ResetPasswordRequest, db: Session = Depends(get_db)):
    if len(data.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    # OTPs are 6-digit numeric — reject malformed input early to keep brute-force surface small
    otp = (data.token or "").strip()
    if not (len(otp) == 6 and otp.isdigit()):
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    row = db.query(models.SiteSetting).filter(
        models.SiteSetting.key == f"reset:{otp}"
    ).first()

    if not row:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    parts = row.value.split(":", 1)
    if len(parts) != 2:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    user_id, expiry_str = parts
    try:
        expiry = datetime.fromisoformat(expiry_str)
        if datetime.utcnow() > expiry:
            db.delete(row)
            db.commit()
            raise HTTPException(status_code=400, detail="OTP has expired. Please request a new one.")
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid or expired OTP")

    user = db.query(models.User).filter(models.User.id == int(user_id)).first()
    if not user:
        raise HTTPException(status_code=400, detail="User not found")

    user.hashed_password = hash_password(data.new_password)
    db.delete(row)  # One-time use
    db.commit()

    return {"message": "Password reset successfully. You can now log in."}
