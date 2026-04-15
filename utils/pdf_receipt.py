"""
Generate a consultation booking receipt PDF with user inputs + admin-configured T&C.
Uses fpdf2 (pip install fpdf2).
"""

import os
import re
from datetime import datetime
from fpdf import FPDF

RECEIPT_DIR = "uploads/receipts"
os.makedirs(RECEIPT_DIR, exist_ok=True)


def _html_to_plain(html: str) -> str:
    """
    Convert HTML to structured plain text suitable for PDF rendering.
    Preserves headings (as bold markers), lists (as numbered/bulleted), paragraphs.
    """
    if not html:
        return ""

    text = html

    # Headings → bold lines
    text = re.sub(r"<h[1-3][^>]*>", "\n**", text)
    text = re.sub(r"</h[1-3]>", "**\n", text)

    # Bold/strong
    text = re.sub(r"<(strong|b)>", "**", text)
    text = re.sub(r"</(strong|b)>", "**", text)

    # Italic
    text = re.sub(r"<(em|i)>", "_", text)
    text = re.sub(r"</(em|i)>", "_", text)

    # List items — convert to numbered or bulleted
    # First handle <ol> by tracking list type
    # Simple approach: replace <li> with bullet/number markers
    li_counter = [0]

    def _replace_ol_start(m: re.Match) -> str:
        li_counter[0] = 0
        return "\n"

    text = re.sub(r"<ol[^>]*>", _replace_ol_start, text)
    text = re.sub(r"<ul[^>]*>", "\n", text)
    text = re.sub(r"</[ou]l>", "\n", text)

    # Replace <li> — use bullet for simplicity (ordered lists get bullets too)
    text = re.sub(r"<li[^>]*>", "\n  - ", text)
    text = re.sub(r"</li>", "", text)

    # Paragraphs and breaks
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<p[^>]*>", "\n", text)
    text = re.sub(r"</p>", "\n", text)

    # Strip all remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&nbsp;", " ").replace("&quot;", '"')

    # Clean up excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def _write_rich_text(pdf: FPDF, text: str):
    """
    Write text with **bold** markers rendered as actual bold in the PDF.
    """
    lines = text.split("\n")
    for line in lines:
        if not line.strip():
            pdf.ln(3)
            continue

        stripped = line.strip()

        # Heading line (starts and ends with **)
        if stripped.startswith("**") and stripped.endswith("**") and len(stripped) > 4:
            heading = stripped[2:-2]
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "B", 10)
            pdf.set_text_color(123, 30, 30)
            pdf.cell(0, 7, heading, new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.set_font("Helvetica", "", 9)
            continue

        # Regular line — strip markers and write as plain text
        clean_line = stripped.replace("**", "").replace("_", "")
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(w=0, h=5, text=clean_line, new_x="LMARGIN", new_y="NEXT")


def generate_receipt(
    appointment_id: int,
    name: str,
    email: str,
    mobile: str,
    dob: str,
    tob: str,
    birth_place: str,
    problem: str,
    payment_reference: str,
    fee: str,
    booked_on: str,
    consultation_terms: str = "",
) -> str:
    """
    Generate a PDF receipt and return the file path.
    consultation_terms: HTML string from admin settings.
    """
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Header ──
    pdf.set_fill_color(123, 30, 30)  # maroon
    pdf.rect(0, 0, 210, 38, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 22)
    pdf.set_y(8)
    pdf.cell(0, 12, "PITHAM CONSULTATION", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, "Booking Confirmation & Receipt", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(10)

    # ── Booking ID & Date ──
    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(95, 8, f"Booking ID: PITHAM-{appointment_id}", new_x="RIGHT")
    pdf.cell(95, 8, f"Date: {booked_on}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Consultation Details ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Consultation Details", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    details = [
        ("Name", name),
        ("Email", email or "N/A"),
        ("Mobile", mobile),
        ("Date of Birth", dob),
        ("Time of Birth", tob),
        ("Birth Place", birth_place),
        ("Payment Reference", payment_reference or "N/A"),
        ("Consultation Fee", f"Rs. {fee}"),
    ]

    for label, value in details:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(50, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(4)

    # ── Problem Statement ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(0, 7, "Problem Statement / Purpose:", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, problem or "N/A")
    pdf.ln(6)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Terms & Conditions ──
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Terms & Conditions", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    if consultation_terms:
        plain = _html_to_plain(consultation_terms)
        _write_rich_text(pdf, plain)
    else:
        pdf.set_font("Helvetica", "", 9)
        pdf.multi_cell(0, 5, (
            "1. Pitham provides astrology and spiritual consultation by "
            "Shri Mayuresh Vispute Guruji via Zoom.\n"
            "2. Full payment is required before scheduling. Payments are non-refundable.\n"
            "3. Your personal information is kept strictly confidential.\n"
            "4. Astrological guidance is for spiritual purposes only.\n"
            "5. All content is intellectual property of Pitham.\n"
            "6. Pitham's liability is limited to the consultation fee paid."
        ))

    # ── Footer ──
    pdf.ln(8)
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(4)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(100, 100, 100)
    pdf.cell(
        0, 5,
        f"Generated on {datetime.utcnow().strftime('%d %B %Y, %H:%M UTC')} | Pitham Consultation",
        align="C",
    )

    # Save
    filename = f"receipt_{appointment_id}.pdf"
    filepath = os.path.join(RECEIPT_DIR, filename)
    pdf.output(filepath)
    return filepath
