import json
from pydantic import BaseModel, EmailStr, field_validator
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
    permissions: list[str] = []   # mirror of UserOut.permissions for client-side gating


# ── User / Profile ────────────────────────────────────────────────────────────

class UserLookupOut(BaseModel):
    """Minimal user info for cross-section name/email/city resolution.
    Returned by /admin/users/lookup — accessible to any admin/moderator so the
    docs / recordings / broadcasts / user-lists / appointments panels can
    display human-readable names without granting access to the full users
    section (which exposes DOB/TOB/birth_place — sensitive consultation data).
    """
    id: int
    name: str
    email: Optional[str] = None
    mobile: str
    city: Optional[str] = None
    state: Optional[str] = None

    model_config = {"from_attributes": True}


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
    permissions: list[str] = []   # admin section keys; only meaningful for role=="moderator"
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("permissions", mode="before")
    @classmethod
    def _decode_permissions(cls, v):
        """The `permissions` column is stored as a JSON-encoded string in the
        DB. Decode it transparently when the schema is built from an ORM row,
        but also accept already-decoded lists (so the schema is symmetric for
        request bodies and re-validation passes)."""
        if v is None or v == "":
            return []
        if isinstance(v, list):
            return v
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
            except (TypeError, ValueError):
                return []
            return parsed if isinstance(parsed, list) else []
        return []


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


# ── Event (Pitham upcoming events) ───────────────────────────────────────────

class EventCreate(BaseModel):
    title: str
    description: Optional[str] = None
    event_date: str                         # ISO date "YYYY-MM-DD"
    event_time: Optional[str] = None        # "HH:MM"
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    image_url: Optional[str] = None
    is_featured: Optional[bool] = False


class EventUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    event_date: Optional[str] = None
    event_time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    image_url: Optional[str] = None
    is_featured: Optional[bool] = None


class EventOut(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    event_date: str
    event_time: Optional[str] = None
    location: Optional[str] = None
    location_map_url: Optional[str] = None
    image_url: Optional[str] = None
    is_featured: bool = False
    # Decoded `registration_config` JSON. Always present (never null) — the
    # field validator hydrates the empty/disabled default for events that
    # don't have registration set up. Public callers see this and decide
    # whether to render the Register button.
    registration_config: dict = {}
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("registration_config", mode="before")
    @classmethod
    def _decode_registration_config(cls, v):
        from utils.event_fields import parse_config  # local import — cycle-safe
        if isinstance(v, dict):
            return v
        return parse_config(v if isinstance(v, str) else None)


# ── Event registration ──────────────────────────────────────────────────────

class EventRegistrationCreate(BaseModel):
    """Payload sent by the public registration form. The server uses the
    event's saved registration_config to decide which keys are valid and
    which are required. Unknown keys in `field_values` are silently dropped.

    `tier_id` is required when the event has tiers configured; ignored
    otherwise (single-fee mode)."""
    field_values: dict
    tier_id: Optional[str] = None


class EventRegistrationOut(BaseModel):
    id: int
    event_id: int
    user_id: int
    name: str
    email: Optional[str] = None
    mobile: Optional[str] = None
    field_values: dict = {}
    status: str
    payment_status: str
    payment_gateway: Optional[str] = None
    payment_reference: Optional[str] = None
    fee_amount: int = 0
    tier_id: Optional[str] = None
    tier_name: Optional[str] = None
    created_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("field_values", mode="before")
    @classmethod
    def _decode_field_values(cls, v):
        if isinstance(v, dict):
            return v
        if isinstance(v, str) and v:
            try:
                parsed = json.loads(v)
                return parsed if isinstance(parsed, dict) else {}
            except (TypeError, ValueError):
                return {}
        return {}


class EventRegistrationInitResult(BaseModel):
    """Returned by POST /events/{id}/register. The shape varies by gateway:

      * gateway="phonepe"  → redirect_url set; frontend window.location's away
      * gateway="razorpay" → razorpay_order set; frontend opens checkout popup,
                              then POSTs the verify endpoint with the signed result
      * gateway="free"     → requires_payment_action=False; status already confirmed
      * gateway="manual"   → requires_payment_action=True but neither redirect nor
                              order; frontend just shows "we'll confirm offline"
    """
    registration_id: int
    status: str
    gateway: Optional[str] = None
    requires_payment_action: bool = False
    redirect_url: Optional[str] = None
    razorpay_order: Optional[dict] = None


class RazorpayVerifyRequest(BaseModel):
    """The triple Razorpay's checkout JS hands the frontend after a successful
    payment. The signature is HMAC-SHA256 over `order_id|payment_id` keyed
    with our secret — only the real Razorpay servers can mint it."""
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


# ── Testimonial ──────────────────────────────────────────────────────────────

class TestimonialCreate(BaseModel):
    name: str
    location: Optional[str] = None
    quote: str
    photo_path: Optional[str] = None
    sort_order: Optional[int] = 0
    is_active: Optional[bool] = True


class TestimonialUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    quote: Optional[str] = None
    photo_path: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class TestimonialOut(BaseModel):
    id: int
    name: str
    location: Optional[str] = None
    quote: str
    photo_path: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Pitham Media (banners, videos, instagram) ────────────────────────────────

class PithamMediaCreate(BaseModel):
    kind: str                                # "banner" | "video" | "instagram"
    title: Optional[str] = None
    url: Optional[str] = None
    image_path: Optional[str] = None
    sort_order: Optional[int] = 0
    is_active: Optional[bool] = True


class PithamMediaUpdate(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    image_path: Optional[str] = None
    sort_order: Optional[int] = None
    is_active: Optional[bool] = None


class PithamMediaOut(BaseModel):
    id: int
    kind: str
    title: Optional[str] = None
    url: Optional[str] = None
    image_path: Optional[str] = None
    sort_order: int = 0
    is_active: bool = True
    created_at: datetime

    model_config = {"from_attributes": True}


# ── Broadcasts ───────────────────────────────────────────────────────────────

class BroadcastOut(BaseModel):
    id: int
    title: str
    message: str
    image_path: Optional[str] = None
    target_type: str
    target_list_id: Optional[int] = None
    sent_by_name: Optional[str] = None
    created_at: datetime
    is_read: bool = False           # set per-user when fetched via /broadcasts/my

    model_config = {"from_attributes": True}


class UnreadCount(BaseModel):
    count: int


# ── Pitham CMS bundle (single public fetch) ──────────────────────────────────

class PithamCmsBundle(BaseModel):
    banners: list[PithamMediaOut]
    videos: list[PithamMediaOut]
    instagram: list[PithamMediaOut]
    gallery: list[PithamMediaOut]
    testimonials: list[TestimonialOut]
    featured_events: list[EventOut]
