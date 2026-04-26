"""Centralised password policy.

Single source of truth so register, reset, and any future change-password
endpoint enforce the same rules. Keep the rules explicit (length + classes)
rather than scoring — easier for the user to act on the error message.

Policy:
  - 10–128 chars (cap exists because bcrypt truncates at 72 bytes; we
    reject longer inputs so users don't think extra chars matter)
  - at least 3 of these 4 character classes: lower, upper, digit, symbol
  - reject pure repetitions and trivially weak strings ("password", etc.)
"""

from __future__ import annotations

import re
from fastapi import HTTPException

MIN_LENGTH = 10
MAX_LENGTH = 128

_LOWER = re.compile(r"[a-z]")
_UPPER = re.compile(r"[A-Z]")
_DIGIT = re.compile(r"\d")
_SYMBOL = re.compile(r"[^A-Za-z0-9]")

# Tiny denylist for the worst offenders. A full HIBP integration is the right
# next step; for now this catches the obvious ones so the rule has teeth.
_BLOCKLIST = {
    "password", "password1", "passw0rd", "qwerty1234", "iloveyou1",
    "12345678910", "abc1234567", "admin12345", "welcome123", "letmein123",
    "Password1", "Password123", "Pa$$w0rd", "P@ssw0rd",
}


def validate_password(pw: str) -> None:
    """Raise HTTPException(400) if `pw` doesn't meet policy. No-ops on success."""
    if not isinstance(pw, str):
        raise HTTPException(status_code=400, detail="Password is required")

    if len(pw) < MIN_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be at least {MIN_LENGTH} characters",
        )
    if len(pw) > MAX_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Password must be {MAX_LENGTH} characters or fewer",
        )

    classes = sum(
        1 for rx in (_LOWER, _UPPER, _DIGIT, _SYMBOL) if rx.search(pw)
    )
    if classes < 3:
        raise HTTPException(
            status_code=400,
            detail="Password must include at least 3 of: lowercase, uppercase, digit, symbol",
        )

    # Reject pure single-character repeats ("aaaaaaaaaa") which technically
    # pass length/class checks if the char is a symbol etc.
    if len(set(pw)) <= 2:
        raise HTTPException(
            status_code=400,
            detail="Password is too repetitive",
        )

    if pw.lower() in {p.lower() for p in _BLOCKLIST}:
        raise HTTPException(
            status_code=400,
            detail="Password is too common — please choose a stronger one",
        )
