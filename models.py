from sqlalchemy import Column, Integer, String, DateTime, Boolean, Text, ForeignKey
from sqlalchemy.orm import relationship
from database import Base
from datetime import datetime
import enum


class AppointmentStatus(str, enum.Enum):
    pending = "pending"
    payment_pending = "payment_pending"
    payment_verified = "payment_verified"
    scheduled = "scheduled"
    completed = "completed"
    cancelled = "cancelled"
    rescheduled = "rescheduled"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    email = Column(String(150), unique=True, index=True, nullable=True)
    mobile = Column(String(20), unique=True, index=True, nullable=False)
    dob = Column(String(20), nullable=False)          # date of birth
    tob = Column(String(20), nullable=False)          # time of birth
    birth_place = Column(String(150), nullable=False)
    city = Column(String(100), nullable=False)
    state = Column(String(100), nullable=False)
    country = Column(String(100), nullable=False)
    hashed_password = Column(String(255), nullable=True)   # null for Google-only users
    google_id = Column(String(100), unique=True, nullable=True)
    role = Column(String(20), default="user")         # "user" | "admin" | "moderator"
    is_active = Column(Boolean, default=True)
    email_verified = Column(Boolean, default=False)
    notify_email = Column(Boolean, default=True)
    notify_sms = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    appointments = relationship("Appointment", back_populates="user")
    documents = relationship("Document", back_populates="user", foreign_keys="Document.user_id")
    queries = relationship("Query", back_populates="user")
    recordings = relationship("Recording", back_populates="user")


class Appointment(Base):
    __tablename__ = "appointments"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(150), nullable=False)
    email = Column(String(150), nullable=False)
    mobile = Column(String(20), nullable=False)
    dob = Column(String(20), nullable=False)
    tob = Column(String(20), nullable=False)
    birth_place = Column(String(150), nullable=False)
    problem = Column(Text, nullable=False)
    selfie_path = Column(String(500), nullable=True)
    payment_status = Column(String(20), default="pending")   # pending | paid
    payment_reference = Column(String(150), nullable=True)
    status = Column(String(30), default=AppointmentStatus.pending)
    scheduled_date = Column(String(20), nullable=True)
    scheduled_time = Column(String(20), nullable=True)
    zoom_link = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)                       # admin notes
    analysis_path = Column(String(500), nullable=True)        # consultation analysis image/file
    analysis_notes = Column(Text, nullable=True)
    recording_link = Column(String(500), nullable=True)      # zoom recording or video link
    receipt_path = Column(String(500), nullable=True)        # booking confirmation PDF
    agreed_terms = Column(Text, nullable=True)               # T&C snapshot at booking time
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", back_populates="appointments")


class Document(Base):
    __tablename__ = "documents"

    id = Column(Integer, primary_key=True, index=True)
    # NULL user_id means this is a reusable gallery/template document
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    file_path = Column(String(500), nullable=False)
    uploaded_by = Column(Integer, ForeignKey("users.id"))    # admin user id
    source_template_id = Column(Integer, ForeignKey("documents.id"), nullable=True)
    batch_id = Column(String(64), nullable=True, index=True)    # same uuid for one bulk op
    batch_label = Column(String(200), nullable=True)            # e.g. "List: Morning Group"
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="documents", foreign_keys=[user_id])


class Query(Base):
    __tablename__ = "queries"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    subject = Column(String(200), nullable=False)
    message = Column(Text, nullable=False)
    reply = Column(Text, nullable=True)
    replied_at = Column(DateTime, nullable=True)
    status = Column(String(20), default="open")       # open | answered
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="queries")


class UserList(Base):
    __tablename__ = "user_lists"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    description = Column(String(500), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    members = relationship("UserListMember", back_populates="user_list", cascade="all, delete-orphan")


class UserListMember(Base):
    __tablename__ = "user_list_members"

    id = Column(Integer, primary_key=True, index=True)
    user_list_id = Column(Integer, ForeignKey("user_lists.id", ondelete="CASCADE"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user_list = relationship("UserList", back_populates="members")
    user = relationship("User")


class SiteSetting(Base):
    """Key-value store for admin-configurable settings."""
    __tablename__ = "site_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Recording(Base):
    __tablename__ = "recordings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    appointment_id = Column(Integer, ForeignKey("appointments.id"), nullable=True)
    title = Column(String(200), nullable=False)
    zoom_recording_url = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", back_populates="recordings")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, index=True)
    admin_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(100), nullable=False)         # e.g. "assign_slot", "mark_completed"
    entity_type = Column(String(50), nullable=True)      # e.g. "appointment", "document", "user"
    entity_id = Column(Integer, nullable=True)
    details = Column(Text, nullable=True)                # JSON or plain text
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class Event(Base):
    """Upcoming Pitham events shown on the public /pitham page."""
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    event_date = Column(String(20), nullable=False, index=True)   # ISO date, e.g. "2026-05-12"
    event_time = Column(String(20), nullable=True)                # e.g. "18:30"
    location = Column(String(200), nullable=True)
    image_url = Column(String(500), nullable=True)                # external URL OR uploaded file path
    is_featured = Column(Boolean, default=False, nullable=False)  # admin-marked highlight
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Testimonial(Base):
    """Devotee testimonials shown on the public /pitham page."""
    __tablename__ = "testimonials"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    location = Column(String(150), nullable=True)
    quote = Column(Text, nullable=False)
    photo_path = Column(String(500), nullable=True)
    sort_order = Column(Integer, default=0, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PithamMedia(Base):
    """Banners, videos/podcasts, and Instagram posts shown on the public /pitham page."""
    __tablename__ = "pitham_media"

    id = Column(Integer, primary_key=True, index=True)
    kind = Column(String(20), nullable=False, index=True)         # "banner" | "video" | "instagram"
    title = Column(String(200), nullable=True)                    # caption (banner) or title (video)
    url = Column(String(1000), nullable=True)                     # video URL / instagram post URL
    image_path = Column(String(500), nullable=True)               # uploaded image (banners; optional video thumbnail)
    sort_order = Column(Integer, default=0, nullable=False, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PendingSettingChange(Base):
    """Settings changes submitted by moderators, awaiting super admin approval."""
    __tablename__ = "pending_setting_changes"

    id = Column(Integer, primary_key=True, index=True)
    submitted_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    key = Column(String(100), nullable=False)
    value = Column(Text, nullable=False)
    status = Column(String(20), default="pending")  # pending | approved | rejected
    reviewed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
