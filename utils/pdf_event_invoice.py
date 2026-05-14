"""
Event registration tax/payment invoice PDF.

Mirrors utils/pdf_invoice (consultation invoice) — simple bill format with a
single line item. Used for registrations that paid through Razorpay /
PhonePe; manual confirmations also get one with the admin-supplied reference.

Generated on demand only — never auto-generated. Booker downloads from My
Events; admin can also regenerate from the registrations dashboard later if
we add that flow.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Optional

from fpdf import FPDF


INVOICE_DIR = "uploads/event_invoices"
os.makedirs(INVOICE_DIR, exist_ok=True)


def _safe(text) -> str:
    """Latin-1 fold for fpdf2's default Helvetica. See pdf_event_receipt for
    rationale. Without this, em-dash literals or any Devanagari user input
    would crash the PDF render."""
    if text is None:
        return ""
    return str(text).encode("latin-1", errors="replace").decode("latin-1")


DASH = "-"


def generate_event_invoice(
    *,
    registration_id: int,
    event_id: int,
    event_title: str,
    tier_name: Optional[str],
    attendee_name: str,
    attendee_email: Optional[str],
    attendee_mobile: Optional[str],
    fee_amount: int,
    payment_gateway: Optional[str],
    payment_id: Optional[str],
    booked_on: str,
    invoice_number: str = "",
    attendee_role: str = "self",
    booker_name: Optional[str] = None,
) -> str:
    """Render the invoice PDF and return its on-disk path. The caller
    typically opens this URL directly in a new tab; we don't persist the
    path on the row (matches the consultation invoice flow — invoice
    generation is on-demand and the file is overwritten on regen)."""
    if not invoice_number:
        # Includes both event id + registration id so admins sorting through
        # invoices can see at a glance which event they belong to.
        invoice_number = f"EVT-{event_id:04d}-{registration_id:06d}"

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Header band ──
    pdf.set_fill_color(123, 30, 30)
    pdf.rect(0, 0, 210, 46, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_y(7)
    pdf.cell(0, 8, "SHRI PITAMBARA BAGLAMUKHI SHAKTI PITHAM", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(0, 6, "AHILYANAGAR", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, "Event Payment Invoice", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(12)

    # ── Invoice details row ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 7, f"Invoice No: {_safe(invoice_number)}", new_x="RIGHT")
    pdf.cell(95, 7, f"Date: {_safe(booked_on)}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(8)

    # ── Bill-to ──
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Bill To", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    bill_to = [
        ("Name", _safe(attendee_name) or DASH),
        ("Email", _safe(attendee_email) or DASH),
        ("Mobile", _safe(attendee_mobile) or DASH),
    ]
    for label, value in bill_to:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(35, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    if attendee_role == "other" and booker_name:
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(100, 100, 100)
        pdf.cell(0, 6, f"Booked by: {_safe(booker_name)}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

    pdf.ln(8)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Items table ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(255, 248, 237)

    col_w = [15, 95, 40, 40]
    headers = ["#", "Description", "Txn Reference", "Amount"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 10, h, border=1, fill=True, align="C", new_x="RIGHT")
    pdf.ln()

    description = _safe(event_title) or "Event Registration"
    if tier_name:
        description = f"{description} - {_safe(tier_name)}"
    # FPDF cells don't auto-shrink long strings; trim to keep the row clean.
    # Plain "..." (not the U+2026 ellipsis) so we stay Latin-1 safe.
    if len(description) > 60:
        description = description[:57] + "..."

    pdf.set_font("Helvetica", "", 10)
    pdf.cell(col_w[0], 10, "1", border=1, align="C", new_x="RIGHT")
    pdf.cell(col_w[1], 10, description, border=1, new_x="RIGHT")
    pdf.cell(col_w[2], 10, _safe(payment_id) or "N/A", border=1, align="C", new_x="RIGHT")
    pdf.cell(col_w[3], 10, f"Rs. {fee_amount}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)

    # ── Total row ──
    pdf.set_font("Helvetica", "B", 11)
    total_x = col_w[0] + col_w[1] + col_w[2]
    pdf.cell(total_x, 10, "Total", border=1, align="R", new_x="RIGHT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(col_w[3], 10, f"Rs. {fee_amount}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)

    # ── Payment status ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(46, 125, 50)
    pdf.cell(0, 8, "PAYMENT STATUS: PAID", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    pdf.ln(4)

    # ── Transaction details ──
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    if payment_gateway:
        pdf.cell(0, 6, f"Gateway: {_safe(payment_gateway).title()}", new_x="LMARGIN", new_y="NEXT")
    if payment_id:
        pdf.cell(0, 6, f"Transaction Reference: {_safe(payment_id)}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Booking ID: SPBSP-EVT-{registration_id}", new_x="LMARGIN", new_y="NEXT")

    # ── Footer ──
    pdf.ln(8)
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(0, 5, "This is a computer-generated invoice and does not require a signature.", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(
        0, 5,
        f"Generated on {datetime.utcnow().strftime('%d %B %Y, %H:%M UTC')} | Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar",
        align="C",
    )

    filename = f"event_invoice_{registration_id}.pdf"
    filepath = os.path.join(INVOICE_DIR, filename)
    pdf.output(filepath)
    return filepath
