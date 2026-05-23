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
from pydantic import BaseModel
from sqlalchemy import desc
from sqlalchemy.orm import Session


from database import get_db
import models
import schemas
from utils.audit import log_action
from utils.auth import get_current_user, require_email_verified
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




def _set_order_id(reg: models.EventRegistration, order_id: str) -> None:
    """Stamp the gateway order_id on a registration at init time. Also
    mirrors to payment_reference so legacy admin code keeps working until
    the frontend is migrated to read payment_order_id directly."""
    reg.payment_order_id = order_id
    reg.payment_reference = order_id




def _set_payment_id(reg: models.EventRegistration, payment_id: str) -> None:
    """Stamp the gateway payment_id on a registration after success. Mirrors
    to payment_reference (post-success it always wins over order_id) for
    legacy reads. Does NOT clear payment_order_id — both are kept so the
    audit trail shows what we initiated AND what was paid."""
    reg.payment_id = payment_id
    reg.payment_reference = payment_id




def _generate_event_receipt(
    event: models.Event,
    reg: models.EventRegistration,
    db: Session,
) -> Optional[str]:
    """Render the registration receipt PDF and persist its path on the row.
    Best-effort: any failure is logged but never raised — receipt generation
    must never roll back a successful payment confirmation. Returns the
    persisted path on success.


    Caller MUST only invoke this after the row has been verified paid.
    """
    if reg.payment_status != "paid":
        return None
    try:
        from utils.pdf_event_receipt import generate_event_receipt
        # Decode field_values JSON (the column stores raw JSON text).
        try:
            fields = json.loads(reg.field_values) if reg.field_values else {}
        except (TypeError, ValueError):
            fields = {}
        # Booker name surfaces only on "other" rows; cheap lookup, can be
        # null if the user record was deleted (DPDP delete) — receipt still
        # renders with attendee details only.
        booker_name = None
        if reg.attendee_role == "other":
            booker = db.query(models.User).filter(models.User.id == reg.user_id).first()
            booker_name = booker.name if booker else None


        booked_on = (
            reg.created_at.strftime("%d %B %Y")
            if reg.created_at
            else datetime.utcnow().strftime("%d %B %Y")
        )
        path = generate_event_receipt(
            registration_id=reg.id,
            event_title=event.title,
            event_date=event.event_date,
            event_time=event.event_time,
            event_location=event.location,
            tier_name=reg.tier_name,
            attendee_name=reg.name or "",
            attendee_email=reg.email,
            attendee_mobile=reg.mobile,
            field_values=fields,
            fee_amount=int(reg.fee_amount or 0),
            payment_gateway=reg.payment_gateway,
            payment_id=reg.payment_id,
            payment_order_id=reg.payment_order_id,
            attendee_role=reg.attendee_role or "self",
            booker_name=booker_name,
            booked_on=booked_on,
        )
        reg.receipt_path = path
        return path
    except Exception as e:
        logger.warning("event receipt generation failed reg=%s: %s", reg.id, e)
        return None




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
    # Gated behind email verification so a fresh-signup user can't pay for
    # an event before we've confirmed they own the email on file.
    user: models.User = Depends(require_email_verified),
    db: Session = Depends(get_db),
):
    # Serialise concurrent /register calls for the same event by taking a
    # row-level lock on the Event row before counting seats. Two users
    # racing for the last seat used to both pass `.count() < cap` and both
    # insert; with `with_for_update()` the second one blocks until the
    # first commits, sees the updated count, and lands on the waitlist (or
    # gets a "fully booked" 400 if waitlist is off). The lock releases on
    # commit / rollback at the end of this request.
    event = (
        db.query(models.Event)
        .filter(models.Event.id == event_id)
        .with_for_update()
        .first()
    )
    if not event:
        raise HTTPException(status_code=404, detail="Event not found")
    config = parse_config(event.registration_config)


    _ensure_registration_open(event, config)


    # ── Retry path — re-fire payment for a specific pending row ───────────
    #
    # When the user clicks "Complete payment" on My Events, the frontend
    # passes ?reg_id=N → reaches us as data.reg_id. We look up THAT exact
    # row, refuse if it's already paid (defends against a stale UI clicking
    # the button after a webhook quietly confirmed the row), and re-issue
    # the gateway with the live fee/gateway from the current event config.
    #
    # We deliberately do NOT auto-detect "user has a pending row, retry it
    # instead of creating a fresh one" — that broke the multi-attendee use
    # case where the same booker registers self in tier A then submits the
    # form again to register their wife in tier B. Retry has to be explicit.
    if data.reg_id is not None:
        existing = (
            db.query(models.EventRegistration)
            .filter(
                models.EventRegistration.id == data.reg_id,
                models.EventRegistration.event_id == event_id,
                models.EventRegistration.user_id == user.id,
            )
            .first()
        )
        if not existing:
            raise HTTPException(status_code=404, detail="Registration not found")
        # Ground-truth the status BEFORE doing anything irreversible. If a
        # webhook landed in the background and confirmed the row, return
        # success with the existing IDs instead of opening another payment.
        # This is the "people are trusting us with money" path — never
        # double-charge a user who already paid.
        if existing.payment_status == "paid" or existing.status == "confirmed":
            return schemas.EventRegistrationInitResult(
                registration_id=existing.id,
                status=existing.status,
                gateway=existing.payment_gateway,
                requires_payment_action=False,
            )
        if existing.status not in ("pending_payment",):
            raise HTTPException(
                status_code=400,
                detail=f"Cannot complete payment for a registration in status '{existing.status}'.",
            )
        if existing.payment_gateway not in ("phonepe", "razorpay"):
            raise HTTPException(
                status_code=400,
                detail="This registration does not use an online gateway.",
            )


        # Re-resolve fee + gateway from the live event/tier config. The
        # snapshot on the row stays for receipt/audit purposes, but a fresh
        # payment attempt always uses the live values — otherwise an admin
        # who corrected the price after the row was created would charge
        # the stale amount.
        current_fee = int(existing.fee_amount or 0)
        current_gateway = existing.payment_gateway
        if existing.tier_id:
            tier = find_tier(config, existing.tier_id)
            if tier:
                current_fee = int(tier.get("fee", 0) or 0)
        else:
            current_fee = int(config.get("fee", 0) or 0)
        current_gateway = config.get("gateway") or current_gateway
        if current_fee == 0:
            # Event went free between init and retry — confirm and skip gateway.
            existing.fee_amount = 0
            existing.payment_status = "n/a"
            existing.payment_gateway = "free"
            existing.status = "confirmed"
            db.commit()
            db.refresh(existing)
            _send_confirmation_email(event, existing, config)
            db.commit()
            return schemas.EventRegistrationInitResult(
                registration_id=existing.id,
                status=existing.status,
                gateway="free",
                requires_payment_action=False,
            )
        try:
            result = initiate_event_payment(
                db,
                gateway=current_gateway,
                registration_id=existing.id,
                amount_rupees=current_fee,
                user_mobile=user.mobile or "",
            )
            existing.fee_amount = current_fee
            existing.payment_gateway = current_gateway
            if result.reference:
                _set_order_id(existing, result.reference)
            # Fresh init = brand-new order. Clear any stale payment_id from
            # an earlier abandoned attempt so the replay-defence lookup on
            # verify never mismatches against a leftover ID.
            existing.payment_id = None
            db.commit()
            log_action(
                db, user.id, "event_registration_payment_retry", "event_registration", existing.id,
                f"gateway={current_gateway} fee={current_fee}",
            )
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


    # Validate the attendee role, defaulting to "self" on anything unexpected.
    # "self" → prefill from booker's profile when the form omits a field.
    # "other" → no profile fallback; the booker is registering someone else,
    #           we must not leak the booker's name/email/mobile onto a row
    #           that's actually about a different person.
    attendee_role = (data.attendee_role or "self").strip().lower()
    if attendee_role not in ("self", "other"):
        attendee_role = "self"


    if attendee_role == "self":
        name   = (cleaned.pop("name", None)   or user.name   or "").strip()
        email  = (cleaned.pop("email", None)  or user.email  or "").strip() or None
        mobile = (cleaned.pop("mobile", None) or user.mobile or "").strip() or None
    else:
        name   = (cleaned.pop("name", None)   or "").strip()
        email  = (cleaned.pop("email", None)  or "").strip() or None
        mobile = (cleaned.pop("mobile", None) or "").strip() or None


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
            attendee_role=attendee_role,
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
        attendee_role=attendee_role,
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
        _set_order_id(reg, result.reference)
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




# NOTE: The previous /events/registrations/{reg_id}/razorpay-verify endpoint
# has been removed. Razorpay confirmations now flow exclusively through the
# webhook at /payments/razorpay/webhook. The frontend redirects to a "we're
# confirming your payment" view after the popup closes and polls the
# read-only status endpoint until the webhook flips the row.




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




# ── User: receipt + invoice ────────────────────────────────────────────────


@router.post("/events/registrations/{reg_id}/generate-receipt")
def user_generate_event_receipt(
    reg_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return the existing receipt path for a paid registration, or
    (re)generate it if missing. Refuses for unpaid rows — receipts only
    exist for actually-paid registrations.


    Idempotent: calling it on an already-receipted row just returns the
    existing path. The PDF on disk is overwritten on regen so the booker's
    download URL stays stable.
    """
    reg = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.id == reg_id,
            models.EventRegistration.user_id == user.id,
        )
        .first()
    )
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.payment_status != "paid":
        raise HTTPException(
            status_code=400,
            detail="Receipt is only available after payment is confirmed.",
        )
    event = _get_event_or_404(db, reg.event_id)
    path = _generate_event_receipt(event, reg, db)
    if not path:
        raise HTTPException(status_code=500, detail="Could not generate receipt right now. Please try again shortly.")
    db.commit()
    return {"receipt_path": path}




@router.post("/events/registrations/{reg_id}/generate-invoice")
def user_generate_event_invoice(
    reg_id: int,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """On-demand invoice generation for the booker. Like the consultation
    flow we don't persist the path on the row — generation is cheap and
    overwriting keeps the URL stable. Refuses for unpaid rows."""
    reg = (
        db.query(models.EventRegistration)
        .filter(
            models.EventRegistration.id == reg_id,
            models.EventRegistration.user_id == user.id,
        )
        .first()
    )
    if not reg:
        raise HTTPException(status_code=404, detail="Registration not found")
    if reg.payment_status != "paid":
        raise HTTPException(
            status_code=400,
            detail="Invoice is only available after payment is confirmed.",
        )
    event = _get_event_or_404(db, reg.event_id)
    booker_name = None
    if reg.attendee_role == "other":
        booker = db.query(models.User).filter(models.User.id == reg.user_id).first()
        booker_name = booker.name if booker else None
    booked_on = (
        reg.created_at.strftime("%d %B %Y")
        if reg.created_at
        else datetime.utcnow().strftime("%d %B %Y")
    )
    try:
        from utils.pdf_event_invoice import generate_event_invoice
        path = generate_event_invoice(
            registration_id=reg.id,
            event_id=event.id,
            event_title=event.title,
            tier_name=reg.tier_name,
            attendee_name=reg.name or "",
            attendee_email=reg.email,
            attendee_mobile=reg.mobile,
            fee_amount=int(reg.fee_amount or 0),
            payment_gateway=reg.payment_gateway,
            payment_id=reg.payment_id,
            booked_on=booked_on,
            attendee_role=reg.attendee_role or "self",
            booker_name=booker_name,
        )
    except Exception as e:
        logger.warning("event invoice generation failed reg=%s: %s", reg.id, e)
        raise HTTPException(status_code=500, detail="Could not generate invoice right now. Please try again shortly.")
    return {"invoice_path": path}




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
    # Look up by payment_order_id (the PhonePe merchant_order_id stamped at
    # init time). Falls back to payment_reference for old rows that haven't
    # been backfilled yet.
    reg = (
        db.query(models.EventRegistration)
        .filter(
            (models.EventRegistration.payment_order_id == txn)
            | (models.EventRegistration.payment_reference == txn),
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
        # Amount-tamper defence: verify PhonePe actually charged the fee we
        # snapshotted on the registration. If the user somehow got an order
        # created for a smaller amount, we will NOT mark the registration
        # paid even though PhonePe says COMPLETED.
        expected_paise = int(reg.fee_amount or 0) * 100
        actual_paise = int(status.get("amount_paise") or 0)
        if expected_paise > 0 and actual_paise > 0 and actual_paise < expected_paise:
            logger.error(
                "phonepe event amount mismatch reg=%s txn=%s expected_paise=%s actual_paise=%s",
                reg.id, txn, expected_paise, actual_paise,
            )
            return {"success": False, "state": "AMOUNT_MISMATCH", "registration_id": reg.id}


        reg.payment_status = "paid"
        # PhonePe doesn't expose a separate payment_id distinct from the
        # merchant_order_id — treat both as the same. payment_order_id is
        # already set from init; populate payment_id with txn so replay
        # defence has a value to match against, and keep payment_reference
        # mirrored.
        _set_payment_id(reg, txn)
        reg.status = "confirmed"
        db.commit()
        db.refresh(reg)
        event = _get_event_or_404(db, reg.event_id)
        config = parse_config(event.registration_config)
        _generate_event_receipt(event, reg, db)
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


class ManualConfirmRequest(BaseModel):
    """Admin must record proof of the offline payment so the audit log isn't
    just `event_registration_confirm_manual` with no context. `reference` is
    free-form (UPI ref, cheque #, "cash receipt 042", etc.) and persisted on
    the registration row; `note` is optional internal text shown on the
    detail dialog."""
    reference: str
    note: Optional[str] = None




@router.post("/admin/event-registrations/{reg_id}/confirm-manual")
def admin_confirm_manual_payment(
    reg_id: int,
    data: ManualConfirmRequest,
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


    # Require a non-empty reference. Without this any moderator can flip a
    # row to paid with no paper trail beyond the audit-log timestamp; if
    # their account is compromised, that's a free backdoor.
    reference = (data.reference or "").strip()
    if len(reference) < 3:
        raise HTTPException(
            status_code=400,
            detail="Please record a payment reference (UPI ref, cheque #, receipt #, etc.).",
        )
    if len(reference) > 200:
        raise HTTPException(status_code=400, detail="Reference is too long (max 200 chars).")


    reg.payment_status = "paid"
    # Manual reference becomes the payment_id (treat the admin-supplied
    # value as the post-success identifier — there's no order_id step for
    # offline payments). Mirror to payment_reference for legacy reads.
    _set_payment_id(reg, reference)
    reg.status = "confirmed"
    db.commit()
    db.refresh(reg)


    event = _get_event_or_404(db, reg.event_id)
    config = parse_config(event.registration_config)
    _generate_event_receipt(event, reg, db)
    _send_confirmation_email(event, reg, config)
    db.commit()


    detail = f"ref={reference}"
    if data.note:
        detail += f" note={data.note[:120]}"
    log_action(db, admin.id, "event_registration_confirm_manual", "event_registration", reg.id, detail)
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
