from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime


# ── Auth ──────────────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    name: str
    email: Optional[str] = None       # email or mobile — at least one required
    mobile: str
    dob: str
    tob: str
    birth_place: str
    city: str
    state: str
    country: str
    password: str


class LoginRequest(BaseModel):
    email: Optional[str] = None       # can be email or mobile number
    mobile: Optional[str] = None
    password: str


class GoogleLoginRequest(BaseModel):
    credential: str                   # Google ID token from GSI


class TokenResponse(BaseModel):
    token: str
    role: str
    name: str


# ── User / Profile ────────────────────────────────────────────────────────────

class UserOut(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    mobile: str
    dob: str
    tob: str
    birth_place: str
    city: str
    state: str
    country: str
    role: str
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Appointment ───────────────────────────────────────────────────────────────

class AppointmentOut(BaseModel):
    id: int
    user_id: int
    name: str
    email: str
    mobile: str
    dob: str
    tob: str
    birth_place: str
    problem: str
    selfie_path: Optional[str]
    payment_status: str
    payment_reference: Optional[str]
    status: str
    scheduled_date: Optional[str]
    scheduled_time: Optional[str]
    zoom_link: Optional[str]
    notes: Optional[str]
    analysis_path: Optional[str] = None
    analysis_notes: Optional[str] = None
    recording_link: Optional[str] = None
    receipt_path: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AssignSlotRequest(BaseModel):
    scheduled_date: str
    scheduled_time: str
    zoom_link: str
    notes: Optional[str] = None


class RescheduleRequest(BaseModel):
    scheduled_date: str
    scheduled_time: str
    zoom_link: Optional[str] = None
    reason: Optional[str] = None


class VerifyPaymentRequest(BaseModel):
    payment_reference: str


class CreateZoomMeetingRequest(BaseModel):
    topic: str
    scheduled_date: str
    scheduled_time: str
    duration: int = 45


class ZoomMeetingResponse(BaseModel):
    id: int | None = None
    join_url: str
    start_url: str | None = None
    password: str | None = None


# ── Query ─────────────────────────────────────────────────────────────────────

class QueryCreate(BaseModel):
    subject: str
    message: str


class QueryOut(BaseModel):
    id: int
    user_id: int
    subject: str
    message: str
    reply: Optional[str]
    replied_at: Optional[datetime]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class QueryReply(BaseModel):
    reply: str


# ── Document ──────────────────────────────────────────────────────────────────

class DocumentOut(BaseModel):
    id: int
    user_id: Optional[int] = None
    title: str
    description: Optional[str]
    file_path: str
    batch_id: Optional[str] = None
    batch_label: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AssignFromGalleryRequest(BaseModel):
    gallery_doc_id: int
    user_id: int


class BulkAssignRequest(BaseModel):
    user_ids: list[int]
    gallery_doc_id: Optional[int] = None
    batch_label: Optional[str] = None      # "List: X" or "Bulk: N users"


class BulkAssignResponse(BaseModel):
    assigned_count: int
    skipped: list[str] = []


# ── User Lists ────────────────────────────────────────────────────────────────

class UserListCreate(BaseModel):
    name: str
    description: Optional[str] = None
    user_ids: list[int] = []


class UserListUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None


class UserListMembersUpdate(BaseModel):
    user_ids: list[int]   # full replacement of the members list


class UserListOut(BaseModel):
    id: int
    name: str
    description: Optional[str]
    created_at: datetime
    member_count: int
    member_ids: list[int]

    model_config = {"from_attributes": True}


# ── Recording ─────────────────────────────────────────────────────────────────

class RecordingOut(BaseModel):
    id: int
    user_id: int
    appointment_id: Optional[int]
    title: str
    zoom_recording_url: str
    created_at: datetime

    model_config = {"from_attributes": True}
