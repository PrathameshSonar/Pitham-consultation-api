"""Single source of truth for the event registration form schema.

The frontend mirrors this catalog in `lib/eventFields.ts`. Adding a new
collectable field is a one-line change in both places — no DB migration,
no per-event data rewrite. Validation defaults closed: an unknown key in
a saved `registration_config` is silently dropped on save and ignored at
render time.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Optional

# Catalog tuple: (key, label, type, profile_field_to_prefill_from)
# Order here is the canonical order used by the public form. Frontend rendering
# walks this list in order; saved JSON ordering is irrelevant.
EVENT_FIELD_CATALOG: tuple[tuple[str, str, str, Optional[str]], ...] = (
    ("name",              "Full name",                         "text",     "name"),
    ("email",             "Email",                             "email",    "email"),
    ("mobile",            "Mobile",                            "tel",      "mobile"),
    ("dob",               "Date of birth",                     "date",     "dob"),
    ("tob",               "Time of birth",                     "time",     "tob"),
    ("birth_place",       "Birth place",                       "text",     "birth_place"),
    ("address",           "Address",                           "textarea", None),
    ("city",              "City",                              "text",     "city"),
    ("problem_statement", "What would you like to discuss?",   "textarea", None),
    ("emergency_contact", "Emergency contact",                 "text",     None),
)

FIELD_KEYS = frozenset(k for k, _, _, _ in EVENT_FIELD_CATALOG)

# Allowed gateway keys. "free" is its own gateway because fee==0 is a state we
# want to enforce explicitly rather than infer. Razorpay/GPay are accepted in
# the schema so admins can pre-configure events; the actual integration ships
# in Phase 2 and registrations against an unimplemented gateway 400.
GATEWAYS = ("free", "manual", "phonepe", "razorpay", "gpay")
IMPLEMENTED_GATEWAYS = {"free", "phonepe", "manual", "razorpay"}


def empty_config() -> Dict[str, Any]:
    """Return a registration_config with the default-disabled state. Used
    when an admin opens the dialog on an event that has no config saved."""
    return {
        "enabled": False,
        "fee": 0,
        "gateway": "free",
        "fields": {k: {"enabled": False, "required": False} for k in FIELD_KEYS},
        "max_attendees": None,
        "deadline": None,
        "confirmation_message": "",
        # Waitlist is opt-in. Only meaningful when max_attendees is set; if
        # it's blank the event is uncapped and the waitlist branch never fires.
        "waitlist_enabled": False,
        # Optional registration "options" / tiers (Mukhya Yajmaan ₹11000,
        # Annadan Seva ₹8500…). When non-empty, the simple `fee` field is
        # ignored at registration time — the user picks a tier and that tier's
        # `fee` applies. Per-tier `max_attendees` lets the admin cap each
        # option independently of the global event cap.
        "tiers": [],
    }


def _normalize_tiers(raw: Any) -> list[Dict[str, Any]]:
    """Coerce admin-submitted tiers into a clean, validated list.

    Drops malformed entries silently rather than 400-ing — admin form may
    transiently include partial rows during edit. Tier IDs are dedupe'd by
    keeping the first occurrence; missing IDs get a stable hash so register-
    time lookups work even if the admin forgot to set one.
    """
    if not isinstance(raw, list):
        return []
    seen_ids: set[str] = set()
    out: list[Dict[str, Any]] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            # A tier without a name is meaningless — skip rather than letting
            # it render as a blank radio button on the public form.
            continue
        try:
            fee = int(item.get("fee", 0) or 0)
            if fee < 0:
                fee = 0
        except (TypeError, ValueError):
            fee = 0
        max_attendees = item.get("max_attendees")
        try:
            max_attendees = int(max_attendees) if max_attendees not in (None, "") else None
            if max_attendees is not None and max_attendees < 1:
                max_attendees = None
        except (TypeError, ValueError):
            max_attendees = None
        try:
            sort_order = int(item.get("sort_order", idx) or 0)
        except (TypeError, ValueError):
            sort_order = idx
        tier_id = str(item.get("id") or "").strip() or f"tier_{idx}_{abs(hash(name)) % 10_000_000}"
        if tier_id in seen_ids:
            continue
        seen_ids.add(tier_id)
        description = str(item.get("description") or "").strip()[:500]
        out.append({
            "id": tier_id,
            "name": name[:150],
            "description": description,
            "fee": fee,
            "max_attendees": max_attendees,
            "sort_order": sort_order,
        })
    out.sort(key=lambda t: (t["sort_order"], t["name"]))
    return out


def find_tier(config: Dict[str, Any], tier_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Look up a tier inside a config by id. Returns None for unknown ids
    or when the config has no tiers."""
    if not tier_id:
        return None
    for t in config.get("tiers") or []:
        if t.get("id") == tier_id:
            return t
    return None


def parse_config(raw: Optional[str]) -> Dict[str, Any]:
    """Decode whatever's stored in `events.registration_config` into a dict.
    Tolerates NULL, empty string, or malformed JSON — returns the default
    'disabled' config in those cases so callers never have to special-case."""
    if not raw:
        return empty_config()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return empty_config()
    if not isinstance(data, dict):
        return empty_config()
    # Merge over defaults so missing keys (e.g. waitlist_enabled on legacy
    # configs saved before that key existed) don't crash readers.
    base = empty_config()
    base.update({k: v for k, v in data.items() if k in base})
    # `fields` needs deeper merge — incoming may be partial.
    incoming_fields = data.get("fields") or {}
    if isinstance(incoming_fields, dict):
        for k in FIELD_KEYS:
            cell = incoming_fields.get(k)
            if isinstance(cell, dict):
                base["fields"][k] = {
                    "enabled": bool(cell.get("enabled", False)),
                    "required": bool(cell.get("required", False)),
                }
    # Defense in depth: re-normalise tiers on read too, so legacy rows that
    # somehow snuck malformed data into the column don't blow up callers.
    base["tiers"] = _normalize_tiers(data.get("tiers"))
    return base


def normalize_config(raw_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Server-side validator. Coerces the admin-submitted config into a
    canonical, safe shape:

      * unknown field keys are dropped
      * required-but-disabled is impossible (required forced to False)
      * fee must be a non-negative int
      * gateway must be in the GATEWAYS set; falls back to 'free'
      * fee==0 forces gateway='free'; gateway='free' forces fee=0
      * max_attendees must be a positive int or null
      * deadline must be an ISO string or null

    Returns the dict to JSON-serialize back into the column.
    """
    if not isinstance(raw_config, dict):
        raw_config = {}

    enabled = bool(raw_config.get("enabled", False))

    try:
        fee = int(raw_config.get("fee", 0) or 0)
        if fee < 0:
            fee = 0
    except (TypeError, ValueError):
        fee = 0

    gateway = raw_config.get("gateway") or "free"
    if gateway not in GATEWAYS:
        gateway = "free"

    # Fee/gateway consistency
    if fee == 0:
        gateway = "free"
    elif gateway == "free" and fee > 0:
        # Non-zero fee with 'free' gateway is incoherent — clamp to free + zero.
        fee = 0

    # Fields — drop unknown keys, force required→false when disabled
    in_fields = raw_config.get("fields") or {}
    out_fields: Dict[str, Dict[str, bool]] = {}
    for key in FIELD_KEYS:
        cell = in_fields.get(key) if isinstance(in_fields, dict) else None
        if not isinstance(cell, dict):
            cell = {}
        is_enabled = bool(cell.get("enabled", False))
        is_required = bool(cell.get("required", False)) and is_enabled
        out_fields[key] = {"enabled": is_enabled, "required": is_required}

    max_attendees = raw_config.get("max_attendees")
    try:
        max_attendees = int(max_attendees) if max_attendees not in (None, "") else None
        if max_attendees is not None and max_attendees < 1:
            max_attendees = None
    except (TypeError, ValueError):
        max_attendees = None

    deadline = raw_config.get("deadline")
    if not isinstance(deadline, str) or not deadline.strip():
        deadline = None

    confirmation_message = raw_config.get("confirmation_message") or ""
    if not isinstance(confirmation_message, str):
        confirmation_message = ""

    waitlist_enabled = bool(raw_config.get("waitlist_enabled", False))
    # Waitlist without a capacity is meaningless — silently turn it off so
    # the runtime branch is unambiguous.
    if not max_attendees:
        waitlist_enabled = False

    tiers = _normalize_tiers(raw_config.get("tiers"))

    # If tiers are present, the single `fee` is informational only — runtime
    # uses the chosen tier's fee. Force gateway off "free" if any tier has a
    # positive fee, otherwise the gateway/fee consistency rule above would
    # accept "free" with a non-zero tier fee.
    if tiers and any(t["fee"] > 0 for t in tiers) and gateway == "free":
        gateway = "phonepe"  # neutral default; admin can override
    if tiers and not any(t["fee"] > 0 for t in tiers):
        # All tiers free — keep gateway at free
        gateway = "free"
        fee = 0

    return {
        "enabled": enabled,
        "fee": fee,
        "gateway": gateway,
        "fields": out_fields,
        "max_attendees": max_attendees,
        "deadline": deadline,
        "confirmation_message": confirmation_message[:1000],
        "waitlist_enabled": waitlist_enabled,
        "tiers": tiers,
    }


def serialize_config(config: Dict[str, Any]) -> str:
    """JSON-dump for storage. Use after `normalize_config`."""
    return json.dumps(config, separators=(",", ":"))


def validate_field_values(config: Dict[str, Any], submitted: Dict[str, Any]) -> Dict[str, Any]:
    """Validate a public registration submission against the event's saved
    config. Returns the cleaned dict to persist as `field_values`. Raises
    ValueError with the human-readable reason on the first failed field."""
    if not isinstance(submitted, dict):
        submitted = {}

    cleaned: Dict[str, Any] = {}
    fields_cfg = config.get("fields") or {}

    for key in FIELD_KEYS:
        cell = fields_cfg.get(key) or {}
        if not cell.get("enabled"):
            continue
        raw = submitted.get(key)
        value = "" if raw is None else str(raw).strip()
        if not value and cell.get("required"):
            raise ValueError(f"{_human_label(key)} is required")
        if value:
            cleaned[key] = value[:2000]
    return cleaned


def _human_label(key: str) -> str:
    for k, label, _, _ in EVENT_FIELD_CATALOG:
        if k == key:
            return label
    return key
