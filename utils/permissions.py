"""Section-based authorization for the admin panel.

Single source of truth for what admin sections exist. Adding a new section is
as simple as appending a key to ADMIN_SECTIONS and using `require_section(...)`
on the router that owns it.

Authorization model:
- role == "admin"     → super admin, bypasses every section check.
- role == "moderator" → must have the section key in its `permissions` list.
- role == "user"      → no admin access.
"""

import json
import logging
from typing import Iterable

from fastapi import Depends, HTTPException
from sqlalchemy.orm import Session

import models
from database import get_db
from utils.auth import get_current_user

logger = logging.getLogger("pitham.permissions")


# ── Section registry — keep in lock-step with frontend `lib/adminSections.ts` ─

# Each section gates one or more admin endpoints / one navbar entry.
ADMIN_SECTIONS: tuple[str, ...] = (
    "appointments",   # /admin/appointments + /admin/calendar (calendar reads appts)
    "users",          # /admin/users
    "user_lists",     # /admin/user-lists
    "documents",      # /admin/documents
    "recordings",     # /admin/recordings
    "queries",        # /admin/queries
    "broadcasts",     # /admin/broadcasts
    "pitham_cms",     # /admin/pitham (banners, events, gallery, testimonials, …)
)


def is_super_admin(user: models.User) -> bool:
    return user.role == "admin"


def get_user_permissions(user: models.User) -> list[str]:
    """Read the JSON-encoded permissions list off the user row.

    Tolerates legacy rows where the column is NULL/empty/malformed by
    returning an empty list — never raises, so a bad row can't lock anyone out.
    Super admins ignore this list at runtime, so corruption only affects
    moderators (who can be re-permissioned by the super admin).
    """
    raw = getattr(user, "permissions", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("Malformed permissions JSON for user_id=%s: %r", user.id, raw)
        return []
    if not isinstance(parsed, list):
        return []
    # Filter to known sections only; an unknown key is never treated as a grant.
    return [s for s in parsed if isinstance(s, str) and s in ADMIN_SECTIONS]


def serialize_permissions(perms: Iterable[str]) -> str:
    """Validate + JSON-encode for storage. Drops unknown keys silently."""
    clean = sorted({p for p in perms if p in ADMIN_SECTIONS})
    return json.dumps(clean)


def has_section_access(user: models.User, section: str) -> bool:
    if is_super_admin(user):
        return True
    if user.role != "moderator":
        return False
    return section in get_user_permissions(user)


def require_section(section: str):
    """Dependency factory: gate a route to a specific admin section.

    Usage:
        @router.get("/foo", dependencies=[Depends(require_section("documents"))])
    or to also receive the user:
        def handler(admin: models.User = Depends(require_section("documents"))):
    """
    if section not in ADMIN_SECTIONS:
        raise ValueError(f"Unknown admin section: {section!r}")

    def _dep(user: models.User = Depends(get_current_user)) -> models.User:
        if is_super_admin(user):
            return user
        if user.role != "moderator":
            raise HTTPException(status_code=403, detail="Admin access required")
        if section not in get_user_permissions(user):
            raise HTTPException(
                status_code=403,
                detail=f"You do not have permission for the '{section}' section",
            )
        return user

    return _dep
