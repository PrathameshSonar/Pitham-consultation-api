"""Event registration endpoints — public registration + admin management.

Routes:
    POST  /events/{id}/register             — user submits the configured form
    GET   /events/{id}/registration         — current user's own registration row
    GET   /me/event-registrations           — list of current user's registrations
    GET   /events/registrations/payment-status?txn=... — poll PhonePe after redirect
    GET   /admin/events/{id}/registrations  — admin list (gated by pitham_cms)
    POST  /admin/event-registrations/{id}/confirm-manual  — admin marks manual payment paid
    POST  /admin/event-registrations/{id}/cancel          — admin cancels a registration
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import desc
from sqlalchemy.orm import Session

from database import get_db
import models
import schemas
from utils.audit import log_action
from utils.auth import get_current_user
from utils.email import (
    send_event_registration_confirmation,
    send_event_waitlist_added,
    send_event_waitlist_promoted,
)
from config import settings
from utils.event_fields import find_tier, parse_config, validate_field_values
from utils.event_payments import (
    GatewayError,
    GatewayInitResult,
    check_event_payment,
    initiate_event_payment,
)
from utils import razorpay_gw
from utils.permissions import require_section

logger = logging.getLogger("pitham.events.registrations")

router = APIRouter(tags=["event-registrations"])

# Section gate for the admin endpoints — same section that owns the public
# Pitham CMS, so a moderator with pitham_cms permission can view registrations
# for the events they manage.
_section_admin = require_section("pitham_cms")


# ── Helpers ─────────────────────────────────────────────────────────────────

def _get_event_or_404(db: Session, event_id: int) -> models.Event:
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    return event


def _ensure_registration_open(event: models.Event, config: dict) -> None:
    """Raise 400 if registration isn't currently accepted for this event."""
    if not config.get("enabled"):
        raise HTTPException(status_code=400, detail="Registration is not open for this event.")

    # Past events
    today = datetime.utcnow().date().isoformat()
    if event.event_date < today:
        raise HTTPException(status_code=400, detail="This event has already taken place.")

    # Per-event registration deadline (separate from event_date)
    deadline = config.get("deadline")
    if deadline:
        try:
            dt = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > dt:
                raise HTTPException(status_code=400, detail="Registration for this event has closed.")
        except ValueError:
            # Malformed deadline → ignore rather than fail; admin can fix it.
            pass


SEAT_HOLDING_STATUSES = ("confirmed", "pending_payment", "attended")


def _capacity_status(db: Session, event_id: int, config: dict) -> tuple[bool, int, Optional[int]]:
    """Return (is_full, registered_count, cap). is_full=False with cap=None
    means capacity is uncapped — never full.

    Counts: confirmed + pending_payment + attended. Waitlist + cancelled rows
    don't count toward capacity (a waitlist signup occupies no real spot).
    """
    cap = config.get("max_attendees")
    if not cap:
        return False, 0, None
    registered = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.status.in_(SEAT_HOLDING_STATUSES),
        )
        .count()
    )
    return registered >= int(cap), registered, int(cap)


def _tier_capacity_status(
    db: Session, event_id: int, tier: dict
) -> tuple[bool, int, Optional[int]]:
    """Same as `_capacity_status` but scoped to a single tier. Used when an
    event has multiple registration options and each option has its own
    headcount (e.g. only 1 Mukhya Yajmaan). Tiers without `max_attendees`
    are uncapped within the global event cap."""
    cap = tier.get("max_attendees")
    if not cap:
        return False, 0, None
    registered = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.tier_id == tier["id"],
            models.EventRegistration.status.in_(SEAT_HOLDING_STATUSES),
        )
        .count()
    )
    return registered >= int(cap), registered, int(cap)


def _promote_oldest_waitlist(db: Session, event_id: int) -> Optional[models.EventRegistration]:
    """Find the oldest waitlist entry for this event and move it up.

    For free events the promoted row goes straight to "confirmed".
    For paid events it goes to "pending_payment" with the current fee snapshot —
    the promoted user must come back and complete payment to keep their seat.
    Either way we email them. Returns the promoted row, or None if the
    waitlist was empty.

    Idempotent: caller is responsible for triggering this only when a real
    seat opens up (e.g. inside a cancel handler). Otherwise we'd over-promote.
    """
    event = db.query(models.Event).filter(models.Event.id == event_id).first()
    if not event:
        return None
    config = parse_config(event.registration_config)
    if not config.get("waitlist_enabled"):
        return None

    # Re-check capacity — somebody else might have re-registered between the
    # cancel and us. If we're already full, leave the waitlist intact.
    is_full, _, _ = _capacity_status(db, event_id, config)
    if is_full:
        return None

    waitlist_row = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.status == "waitlist",
        )
        .order_by(models.EventRegistration.created_at.asc())
        .first()
    )
    if not waitlist_row:
        return None

    fee = int(config.get("fee", 0) or 0)
    waitlist_row.fee_amount = fee
    if fee > 0:
        # Paid event: the promoted user must finish payment via the same
        # registration page (handler re-fires the gateway when an existing
        # pending_payment row is found).
        waitlist_row.status = "pending_payment"
        waitlist_row.payment_status = "pending"
    else:
        waitlist_row.status = "confirmed"
        waitlist_row.payment_status = "n/a"
    db.commit()
    db.refresh(waitlist_row)

    # Best-effort notification — never raise from here so a flaky mailer
    # doesn't undo the promotion.
    try:
        event_url = f"{settings.core.frontend_url}/pitham/events/{event_id}/register"
        send_event_waitlist_promoted(
            to=waitlist_row.email or "",
            name=waitlist_row.name or "",
            event_title=event.title,
            event_date=event.event_date,
            fee_amount=fee,
            needs_payment=fee > 0,
            event_url=event_url,
            mobile=waitlist_row.mobile or "",
        )
        # Free events also get the "registration confirmed" email — same one
        # we send on initial confirm — so the user has the full event detail.
        if fee == 0:
            _send_confirmation_email(event, waitlist_row, config)
            db.commit()
    except Exception as e:
        logger.warning("waitlist promotion email failed reg=%s: %s", waitlist_row.id, e)

    return waitlist_row


def _send_confirmation_email(event: models.Event, reg: models.EventRegistration, config: dict) -> None:
    """Best-effort send. Never raises — confirmation failure shouldn't roll
    back a successful registration."""
    if not reg.email:
        return
    try:
        send_event_registration_confirmation(
            to=reg.email,
            name=reg.name or "",
            event_title=event.title,
            event_date=event.event_date,
            event_time=event.event_time or "",
            location=event.location or "",
            fee_amount=reg.fee_amount or 0,
            payment_status=(
                "paid" if reg.payment_status == "paid"
                else ("pending" if reg.fee_amount and reg.payment_status != "paid" else "n/a")
            ),
            custom_message=config.get("confirmation_message") or "",
            mobile=reg.mobile or "",
        )
        reg.confirmation_sent_at = datetime.utcnow()
    except Exception as e:
        logger.warning("event-confirmation email failed event=%s reg=%s: %s", event.id, reg.id, e)


# ── User: register for an event ─────────────────────────────────────────────

@router.post("/events/{event_id}/register", response_model=schemas.EventRegistrationInitResult)
def register_for_event(
    event_id: int,
    data: schemas.EventRegistrationCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    event = _get_event_or_404(db, event_id)
    config = parse_config(event.registration_config)

    _ensure_registration_open(event, config)

    # One registration per user per event — flipping the registration to
    # "cancelled" then re-registering creates a fresh row, but an already-
    # active row blocks duplicates.
    existing = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.user_id == user.id,
            models.EventRegistration.status.in_(("pending_payment", "confirmed", "attended")),
        )
        .first()
    )
    if existing:
        # If they're stuck in pending_payment for a hosted/popup gateway, kick
        # the gateway again so the user can complete payment without creating
        # a duplicate row. The reference column gets the fresh txn id.
        if existing.status == "pending_payment" and existing.payment_gateway in ("phonepe", "razorpay"):
            try:
                result = initiate_event_payment(
                    db,
                    gateway=existing.payment_gateway,
                    registration_id=existing.id,
                    amount_rupees=existing.fee_amount,
                    user_mobile=user.mobile or "",
                )
                existing.payment_reference = result.reference
                db.commit()
                return schemas.EventRegistrationInitResult(
                    registration_id=existing.id,
                    status=existing.status,
                    gateway=existing.payment_gateway,
                    requires_payment_action=result.requires_payment_action,
                    redirect_url=result.redirect_url,
                    razorpay_order=result.razorpay_order,
                )
            except GatewayError as e:
                raise HTTPException(status_code=400, detail=str(e))
        raise HTTPException(
            status_code=400,
            detail="You're already registered for this event. See My Events in your dashboard.",
        )

    # ── Tier resolution (registration options) ─────────────────────────────
    # Events can be configured with multiple "options" — Mukhya Yajmaan ₹11000,
    # Annadan Seva ₹8500, etc. Frontend sends the chosen tier_id; backend
    # snapshots the fee + tier name onto the row so renaming a tier later
    # never rewrites an attendee's invoice.
    tiers = config.get("tiers") or []
    selected_tier: Optional[dict] = None
    if tiers:
        if not data.tier_id:
            raise HTTPException(status_code=400, detail="Please pick a registration option.")
        selected_tier = find_tier(config, data.tier_id)
        if not selected_tier:
            raise HTTPException(status_code=400, detail="The selected registration option is no longer available.")

    is_full, _registered, _cap = _capacity_status(db, event_id, config)

    # Per-tier capacity check happens whether or not the event is globally
    # full — a tier can be sold out even when the event has spots left in
    # other tiers. We treat tier-full like global-full for the waitlist
    # decision so the user lands somewhere predictable.
    tier_is_full = False
    if selected_tier:
        tier_is_full, _t_reg, _t_cap = _tier_capacity_status(db, event_id, selected_tier)

    blocked_by_capacity = is_full or tier_is_full
    if blocked_by_capacity and not config.get("waitlist_enabled"):
        raise HTTPException(
            status_code=400,
            detail=(
                f"'{selected_tier['name']}' is fully booked." if tier_is_full and selected_tier
                else "This event is fully booked."
            ),
        )
    going_to_waitlist = blocked_by_capacity and bool(config.get("waitlist_enabled"))

    # Validate the submitted form values against the event's config — drops
    # unknown keys, enforces required.
    try:
        cleaned = validate_field_values(config, data.field_values)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Pull canonical contact fields out of the cleaned values so admin lists
    # don't have to dig into JSON. Fall back to the user profile when the
    # form didn't ask for that field.
    name   = (cleaned.pop("name", None)   or user.name   or "").strip()
    email  = (cleaned.pop("email", None)  or user.email  or "").strip() or None
    mobile = (cleaned.pop("mobile", None) or user.mobile or "").strip() or None

    if not name:
        raise HTTPException(status_code=400, detail="Name is required")

    # When an event has tiers, the chosen tier dictates the fee. Otherwise
    # we fall back to the single `fee` field on the config. Snapshot the
    # tier name onto the registration row so the admin / receipt views read
    # fast without re-parsing the JSON config.
    if selected_tier:
        fee = int(selected_tier.get("fee", 0) or 0)
        tier_id_snap: Optional[str] = selected_tier["id"]
        tier_name_snap: Optional[str] = selected_tier["name"]
    else:
        fee = int(config.get("fee", 0) or 0)
        tier_id_snap = None
        tier_name_snap = None
    gateway = config.get("gateway") or "free"
    if fee == 0:
        # All-free tier or no tiers + free fee — bypass any configured gateway.
        gateway = "free"

    # Waitlist branch — event/tier is full but waitlist is on. Don't touch
    # the gateway; the user will pay (if needed) only when promoted.
    if going_to_waitlist:
        reg = models.EventRegistration(
            event_id=event_id,
            user_id=user.id,
            name=name,
            email=email,
            mobile=mobile,
            field_values=json.dumps(cleaned, separators=(",", ":")) if cleaned else None,
            status="waitlist",
            payment_status="n/a",
            payment_gateway=gateway,         # remembered so promotion knows which gateway to use
            fee_amount=0,                    # snapshot zero — real fee captured on promotion
            tier_id=tier_id_snap,
            tier_name=tier_name_snap,
        )
        db.add(reg)
        db.commit()
        db.refresh(reg)
        try:
            send_event_waitlist_added(
                to=email or "",
                name=name,
                event_title=event.title,
                event_date=event.event_date,
                mobile=mobile or "",
            )
        except Exception as e:
            logger.warning("waitlist signup email failed reg=%s: %s", reg.id, e)
        log_action(
            db, user.id, "event_waitlist_join", "event", event.id,
            f"reg={reg.id}",
        )
        return schemas.EventRegistrationInitResult(
            registration_id=reg.id,
            status="waitlist",
            gateway=None,
            requires_payment_action=False,
        )

    reg = models.EventRegistration(
        event_id=event_id,
        user_id=user.id,
        name=name,
        email=email,
        mobile=mobile,
        field_values=json.dumps(cleaned, separators=(",", ":")) if cleaned else None,
        status="pending_payment" if fee > 0 else "confirmed",
        payment_status="pending" if fee > 0 else "n/a",
        payment_gateway=gateway,
        fee_amount=fee,
        tier_id=tier_id_snap,
        tier_name=tier_name_snap,
    )
    db.add(reg)
    db.commit()
    db.refresh(reg)

    # Dispatch payment (or skip for free/manual)
    try:
        result: GatewayInitResult = initiate_event_payment(
            db,
            gateway=gateway,
            registration_id=reg.id,
            amount_rupees=fee,
            user_mobile=mobile or "",
        )
    except GatewayError as e:
        # Roll back the registration so the user can retry without a stale row.
        db.delete(reg)
        db.commit()
        raise HTTPException(status_code=400, detail=str(e))

    if result.reference:
        reg.payment_reference = result.reference
    if not result.requires_payment_action:
        # Free event — settled at creation time.
        reg.status = "confirmed"
        reg.payment_status = "n/a"
        _send_confirmation_email(event, reg, config)
    db.commit()
    db.refresh(reg)

    log_action(
        db, user.id, "event_register", "event", event.id,
        f"reg={reg.id} fee={fee} gateway={gateway}",
    )

    return schemas.EventRegistrationInitResult(
        registration_id=reg.id,
        status=reg.status,
        gateway=reg.payment_gateway,
        requires_payment_action=result.requires_payment_action,
        redirect_url=result.redirect_url,
        razorpay_order=result.razorpay_order,
    )


# ── User: Razorpay signature verification ───────────────────────────────────

@router.post("/events/registrations/{reg_id}/razorpay-verify")
def razorpay_verify(
    reg_id: int,
    data: schemas.RazorpayVerifyRequest,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Called by the frontend after Razorpay's checkout JS reports a
    successful payment. Verifies the HMAC signature against our key_secret —
    only Razorpay's servers can mint a valid one — then flips the
    registration to confirmed and emails the user."""
    reg = (
        db.query(models.EventRegistration)
        .filter(models.EventRegistration.id == reg_id, models.EventRegistration.user_id == user.id)
        .first()
    )
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.payment_gateway != "razorpay":
        raise HTTPException(status_code=400, detail="This registration isn't using Razorpay.")
    if reg.status == "confirmed":
        return {"success": True, "registration_id": reg.id}

    try:
        ok = razorpay_gw.verify_payment_signature(
            db,
            razorpay_order_id=data.razorpay_order_id,
            razorpay_payment_id=data.razorpay_payment_id,
            razorpay_signature=data.razorpay_signature,
        )
    except razorpay_gw.RazorpayError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not ok:
        # Signature mismatch — almost always a tampered request, occasionally
        # a misconfigured key_secret. Either way: do NOT confirm, do not log
        # this as a successful payment.
        logger.warning("razorpay signature mismatch reg=%s order=%s", reg.id, data.razorpay_order_id)
        raise HTTPException(status_code=400, detail="Payment signature could not be verified.")

    reg.payment_status = "paid"
    reg.payment_reference = data.razorpay_payment_id
    reg.status = "confirmed"
    db.commit()
    db.refresh(reg)

    event = _get_event_or_404(db, reg.event_id)
    config = parse_config(event.registration_config)
    _send_confirmation_email(event, reg, config)
    db.commit()

    log_action(
        db, user.id, "event_registration_razorpay_verified", "event_registration", reg.id,
        f"order={data.razorpay_order_id}",
    )
    return {"success": True, "registration_id": reg.id}


# ── User: my registration for a specific event ──────────────────────────────

@router.get("/events/{event_id}/registration", response_model=Optional[schemas.EventRegistrationOut])
def my_registration(
    event_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns the current user's most recent active registration for this
    event, or null if not registered. The event detail page uses this to
    decide whether to show a Register button or a "You're registered" pill."""
    reg = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.event_id == event_id,
            models.EventRegistration.user_id == user.id,
            models.EventRegistration.status.in_(("pending_payment", "confirmed", "attended")),
        )
        .order_by(desc(models.EventRegistration.created_at))
        .first()
    )
    return reg


# ── User: list of all my registrations (drives /dashboard/events) ───────────

@router.get("/me/event-registrations", response_model=List[dict])
def list_my_registrations(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Returns registrations + minimal event metadata so the My Events page
    can render with one round-trip. Newest first."""
    rows = (
        db.query(models.EventRegistration, models.Event)
        .join(models.Event, models.Event.id == models.EventRegistration.event_id)
        .filter(models.EventRegistration.user_id == user.id)
        .order_by(desc(models.EventRegistration.created_at))
        .all()
    )
    out: list[dict] = []
    for reg, event in rows:
        out.append({
            "registration": schemas.EventRegistrationOut.model_validate(reg).model_dump(),
            "event": {
                "id": event.id,
                "title": event.title,
                "event_date": event.event_date,
                "event_time": event.event_time,
                "location": event.location,
                "image_url": event.image_url,
            },
        })
    return out


# ── User: payment-status poll (after PhonePe redirect) ─────────────────────

@router.get("/events/registrations/payment-status")
def event_payment_status(
    txn: str,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Called by the frontend after PhonePe redirects the user back. Looks up
    the registration by txn id, polls PhonePe for the latest state, and
    flips the row to confirmed on success — at which point the confirmation
    email goes out."""
    reg = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.payment_reference == txn,
            models.EventRegistration.user_id == user.id,
        )
        .first()
    )
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found for this transaction")

    if reg.status == "confirmed":
        return {"success": True, "state": "COMPLETED", "registration_id": reg.id}

    try:
        status = check_event_payment(txn)
    except Exception as e:
        logger.warning("payment status check failed for txn=%s: %s", txn, e)
        return {"success": False, "state": "PENDING", "registration_id": reg.id}

    if status.get("success"):
        reg.payment_status = "paid"
        reg.status = "confirmed"
        db.commit()
        db.refresh(reg)
        event = _get_event_or_404(db, reg.event_id)
        config = parse_config(event.registration_config)
        _send_confirmation_email(event, reg, config)
        db.commit()

    return {
        "success": bool(status.get("success")),
        "state": status.get("state", "UNKNOWN"),
        "registration_id": reg.id,
    }


# ── Admin: list registrations for an event ─────────────────────────────────

@router.get("/admin/events/{event_id}/registrations", response_model=List[schemas.EventRegistrationOut])
def admin_list_registrations(
    event_id: int,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    _get_event_or_404(db, event_id)
    return (
        db.query(models.EventRegistration)
        .filter(models.EventRegistration.event_id == event_id)
        .order_by(desc(models.EventRegistration.created_at))
        .all()
    )


# ── Admin: confirm a manual-gateway registration after offline payment ─────

@router.post("/admin/event-registrations/{reg_id}/confirm-manual")
def admin_confirm_manual_payment(
    reg_id: int,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.payment_gateway != "manual":
        raise HTTPException(
            status_code=400,
            detail="Only manual-gateway registrations can be confirmed this way.",
        )
    if reg.status == "confirmed":
        return {"message": "Already confirmed."}
    reg.payment_status = "paid"
    reg.status = "confirmed"
    db.commit()
    db.refresh(reg)

    event = _get_event_or_404(db, reg.event_id)
    config = parse_config(event.registration_config)
    _send_confirmation_email(event, reg, config)
    db.commit()

    log_action(db, admin.id, "event_registration_confirm_manual", "event_registration", reg.id, "")
    return {"message": "Registration confirmed."}


# ── Admin: cancel a registration ────────────────────────────────────────────

@router.post("/admin/event-registrations/{reg_id}/cancel")
def admin_cancel_registration(
    reg_id: int,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    """Cancel a registration. If the cancelled row was occupying a real seat
    (confirmed / pending_payment / attended), try to promote the oldest
    waitlist entry up to fill it. Cancelling a waitlist row never triggers
    promotion — that just trims the queue."""
    reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.status == "cancelled":
        return {"message": "Already cancelled."}

    was_holding_seat = reg.status in ("confirmed", "pending_payment", "attended")
    event_id = reg.event_id

    reg.status = "cancelled"
    reg.cancelled_at = datetime.utcnow()
    db.commit()
    log_action(db, admin.id, "event_registration_cancel", "event_registration", reg.id, "")

    promoted_id: Optional[int] = None
    if was_holding_seat:
        promoted = _promote_oldest_waitlist(db, event_id)
        if promoted:
            promoted_id = promoted.id
            log_action(
                db, admin.id, "event_waitlist_promote", "event_registration", promoted.id,
                f"replaced reg={reg.id}",
            )

    return {
        "message": "Registration cancelled."
        + (f" Promoted waitlist registration #{promoted_id}." if promoted_id else ""),
        "promoted_registration_id": promoted_id,
    }


@router.post("/admin/event-registrations/{reg_id}/promote-waitlist")
def admin_promote_waitlist(
    reg_id: int,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    """Manually promote a specific waitlist row — useful when admin wants
    to reorder or pull a particular person up regardless of queue order
    (e.g. honouring a follow-up phone call).

    Bypasses the auto-promotion's capacity re-check so admin can over-fill
    deliberately. The row's fee/status flips per the current event config."""
    reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.status != "waitlist":
        raise HTTPException(status_code=400, detail="Only waitlist registrations can be promoted.")

    event = _get_event_or_404(db, reg.event_id)
    config = parse_config(event.registration_config)
    fee = int(config.get("fee", 0) or 0)
    reg.fee_amount = fee
    if fee > 0:
        reg.status = "pending_payment"
        reg.payment_status = "pending"
    else:
        reg.status = "confirmed"
        reg.payment_status = "n/a"
    db.commit()
    db.refresh(reg)

    try:
        event_url = f"{settings.core.frontend_url}/pitham/events/{event.id}/register"
        send_event_waitlist_promoted(
            to=reg.email or "",
            name=reg.name or "",
            event_title=event.title,
            event_date=event.event_date,
            fee_amount=fee,
            needs_payment=fee > 0,
            event_url=event_url,
            mobile=reg.mobile or "",
        )
        if fee == 0:
            _send_confirmation_email(event, reg, config)
            db.commit()
    except Exception as e:
        logger.warning("manual waitlist promotion email failed reg=%s: %s", reg.id, e)

    log_action(db, admin.id, "event_waitlist_promote_manual", "event_registration", reg.id, "")
    return {"message": "Promoted from waitlist.", "registration_id": reg.id, "status": reg.status}


# ── Admin: mark attended after the event ────────────────────────────────────

@router.post("/admin/event-registrations/{reg_id}/attended")
def admin_mark_attended(
    reg_id: int,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    reg = db.query(models.EventRegistration).filter(models.EventRegistration.id == reg_id).first()
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    reg.status = "attended"
    reg.attended_at = datetime.utcnow()
    db.commit()
    log_action(db, admin.id, "event_registration_attended", "event_registration", reg.id, "")
    return {"message": "Marked attended."}
