"""
Build a minimal RFC 5545 .ics calendar invite for a scheduled consultation.
No external dependency — fpdf2 doesn't bundle one and `ics` would be the only
caller, so we hand-roll the few lines we need.

Times are stored in IST (Asia/Kolkata) and emitted as floating local times with
a TZID so Google/Apple Calendar render them correctly regardless of the user's
phone time zone.
"""

import os
import uuid
from datetime import datetime, timedelta
from typing import Optional

ICS_DIR = "uploads/invites"
os.makedirs(ICS_DIR, exist_ok=True)

# Vtimezone block for India Standard Time. India doesn't observe DST so this
# is a single, fixed offset block. Hard-coded to keep the dependency surface zero.
IST_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Asia/Kolkata
BEGIN:STANDARD
DTSTART:19700101T000000
TZOFFSETFROM:+0530
TZOFFSETTO:+0530
TZNAME:IST
END:STANDARD
END:VTIMEZONE"""


def _escape(s: str) -> str:
    """Escape special chars per RFC 5545."""
    return (
        (s or "")
        .replace("\\", "\\\\")
        .replace(",", "\\,")
        .replace(";", "\\;")
        .replace("\n", "\\n")
        .replace("\r", "")
    )


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets."""
    if len(line) <= 75:
        return line
    out = [line[:75]]
    rest = line[75:]
    while rest:
        out.append(" " + rest[:74])
        rest = rest[74:]
    return "\r\n".join(out)


def generate_ics(
    appointment_id: int,
    summary: str,
    description: str,
    location: str,
    scheduled_date: str,   # "YYYY-MM-DD"
    scheduled_time: str,   # "HH:MM" or "HH:MM:SS"
    duration_minutes: int = 30,
    organizer_email: Optional[str] = None,
    attendee_email: Optional[str] = None,
) -> Optional[str]:
    """
    Generate an .ics file for the scheduled consultation. Returns the file path,
    or None if the date/time can't be parsed.
    """
    try:
        # Accept HH:MM or HH:MM:SS
        time_str = scheduled_time if len(scheduled_time) == 8 else f"{scheduled_time}:00"
        start = datetime.strptime(f"{scheduled_date} {time_str}", "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return None

    end = start + timedelta(minutes=duration_minutes)

    dtstamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    dtstart = start.strftime("%Y%m%dT%H%M%S")
    dtend = end.strftime("%Y%m%dT%H%M%S")
    uid = f"spbsp-{appointment_id}-{uuid.uuid4().hex[:8]}@spbsp.in"

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SPBSP Ahilyanagar//Consultation//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        IST_VTIMEZONE,
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;TZID=Asia/Kolkata:{dtstart}",
        f"DTEND;TZID=Asia/Kolkata:{dtend}",
        _fold(f"SUMMARY:{_escape(summary)}"),
        _fold(f"DESCRIPTION:{_escape(description)}"),
        _fold(f"LOCATION:{_escape(location)}"),
        "STATUS:CONFIRMED",
        "TRANSP:OPAQUE",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        f"DESCRIPTION:{_escape(summary)}",
        "TRIGGER:-PT30M",
        "END:VALARM",
    ]
    if organizer_email:
        lines.append(_fold(f"ORGANIZER;CN=SPBSP Ahilyanagar:mailto:{organizer_email}"))
    if attendee_email:
        lines.append(_fold(f"ATTENDEE;CN={attendee_email};RSVP=FALSE:mailto:{attendee_email}"))
    lines += ["END:VEVENT", "END:VCALENDAR"]

    body = "\r\n".join(lines) + "\r\n"
    filepath = os.path.join(ICS_DIR, f"appointment_{appointment_id}.ics")
    with open(filepath, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return filepath
