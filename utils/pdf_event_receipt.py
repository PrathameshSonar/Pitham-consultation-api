"""
Event registration receipt PDF.

Mirrors utils/pdf_receipt (the consultation receipt) so admin paperwork looks
consistent across products. The shape we render is:
    - Header band with org name + "Event Registration Receipt"
    - Booking ID + booking date
    - Event details block (title, date, time, location, tier)
    - Attendee details block (name, email, mobile + any extra field_values
      the booker submitted)
    - Payment summary (fee, gateway, payment ID)
    - Optional "Booked by" footer when attendee_role == "other"

NOT included by design: any "terms & conditions" body. Event-specific terms
live on the event itself in the future; for now the receipt stays focused
on the transaction.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, Optional

from fpdf import FPDF


RECEIPT_DIR = "uploads/event_receipts"
os.makedirs(RECEIPT_DIR, exist_ok=True)


def _safe(text: Any) -> str:
    """Coerce arbitrary input into a Latin-1-safe string for fpdf2's default
    Helvetica font. Without this, user-supplied Devanagari (Hindi / Marathi
    names) or even our own em-dash literals crash PDF rendering. Non-encodable
    characters are replaced with '?' rather than dropped — the booker still
    sees that something was there, just not what.

    Switching to a Unicode TTF font is the cleaner long-term fix; this is
    the cheap-and-correct one until that's prioritised.
    """
    if text is None:
        return ""
    s = str(text)
    return s.encode("latin-1", errors="replace").decode("latin-1")


# Dash glyph used in our own placeholder-empty fields. Plain hyphen so it
# survives the Latin-1 path (em-dash U+2014 is not encodable).
DASH = "-"


def _label_for(key: str) -> str:
    """Human-friendly label for snake_case field keys. Mirrors the labels
    used in the My Events frontend so the booker sees the same wording."""
    overrides = {
        "name":              "Name",
        "email":             "Email",
        "mobile":            "Mobile",
        "dob":               "Date of birth",
        "tob":               "Time of birth",
        "birth_place":       "Birth place",
        "address":           "Address",
        "city":              "City",
        "problem_statement": "What you'd like guidance on",
        "emergency_contact": "Emergency contact",
    }
    if key in overrides:
        return overrides[key]
    return key.replace("_", " ").title()


def generate_event_receipt(
    *,
    registration_id: int,
    event_title: str,
    event_date: str,
    event_time: Optional[str],
    event_location: Optional[str],
    tier_name: Optional[str],
    attendee_name: str,
    attendee_email: Optional[str],
    attendee_mobile: Optional[str],
    field_values: Optional[Dict[str, Any]],
    fee_amount: int,
    payment_gateway: Optional[str],
    payment_id: Optional[str],
    payment_order_id: Optional[str],
    attendee_role: str,
    booker_name: Optional[str],
    booked_on: str,
) -> str:
    """Render the receipt PDF and return its on-disk path. Caller persists
    the path on `EventRegistration.receipt_path`. File is overwritten on
    re-generation so the same URL keeps working — but the caller should
    only call this on a verified-paid row."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Header band ──
    pdf.set_fill_color(123, 30, 30)  # brand maroon
    pdf.rect(0, 0, 210, 42, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_y(7)
    pdf.cell(0, 8, "SHRI PITAMBARA BAGLAMUKHI SHAKTI PITHAM", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "AHILYANAGAR", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, "Event Registration Receipt", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(12)

    # ── Booking ID + date ──
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(95, 8, f"Booking ID: SPBSP-EVT-{registration_id}", new_x="RIGHT")
    pdf.cell(95, 8, f"Date: {_safe(booked_on)}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Event details ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Event Details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    event_rows = [
        ("Event", _safe(event_title) or DASH),
        ("Date", _safe(event_date) or DASH),
    ]
    if event_time:
        event_rows.append(("Time", _safe(event_time)))
    if event_location:
        event_rows.append(("Location", _safe(event_location)))
    if tier_name:
        event_rows.append(("Option", _safe(tier_name)))

    for label, value in event_rows:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Attendee details ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Attendee Details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    attendee_rows = [
        ("Name", _safe(attendee_name) or DASH),
        ("Email", _safe(attendee_email) or DASH),
        ("Mobile", _safe(attendee_mobile) or DASH),
    ]
    for label, value in attendee_rows:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    # Free-form configurable fields the event admin asked for.
    if field_values:
        for k, v in field_values.items():
            if k in ("name", "email", "mobile"):
                continue  # already rendered above
            text = _safe(v)
            if not text:
                continue
            pdf.set_font("Helvetica", "B", 10)
            pdf.cell(50, 7, f"{_label_for(k)}:", new_x="RIGHT")
            pdf.set_font("Helvetica", "", 10)
            # Long answers (problem_statement etc.) flow onto multiple lines.
            if len(text) > 60:
                pdf.ln(0)
                pdf.set_x(pdf.l_margin + 50)
                pdf.multi_cell(0, 6, text)
            else:
                pdf.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Payment summary ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Payment", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    payment_rows = [
        ("Amount", f"Rs. {fee_amount}"),
        ("Gateway", _safe(payment_gateway).title() if payment_gateway else DASH),
    ]
    if payment_order_id:
        payment_rows.append(("Order Ref", _safe(payment_order_id)))
    if payment_id:
        payment_rows.append(("Payment Ref", _safe(payment_id)))

    for label, value in payment_rows:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    # ── "Booked by" line for other-attendee rows ──
    # Surfaces the booker's name on the receipt so the family member who
    # actually attends can see who paid for them. Only printed for "other".
    if attendee_role == "other" and booker_name:
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"Booked by: {_safe(booker_name)}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    # ── Footer ──
    pdf.ln(8)
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(
        0, 5,
        f"Generated on {datetime.utcnow().strftime('%d %B %Y, %H:%M UTC')} | Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar",
        align="C",
    )

    filename = f"event_receipt_{registration_id}.pdf"
    filepath = os.path.join(RECEIPT_DIR, filename)
    pdf.output(filepath)
    return filepath
