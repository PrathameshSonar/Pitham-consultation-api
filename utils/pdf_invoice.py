"""
Generate a CA/tax invoice PDF for a consultation payment.
Simple bill receipt format: name, email, mobile, amount, transaction ID, date.
Uses fpdf2.
"""

import os
from datetime import datetime
from fpdf import FPDF

INVOICE_DIR = "uploads/invoices"
os.makedirs(INVOICE_DIR, exist_ok=True)


def generate_invoice(
    appointment_id: int,
    name: str,
    email: str,
    mobile: str,
    payment_reference: str,
    fee: str,
    booked_on: str,
    invoice_number: str = "",
) -> str:
    """
    Generate a payment invoice PDF and return the file path.
    """
    if not invoice_number:
        invoice_number = f"INV-{appointment_id:06d}"

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()

    # ── Header ──
    pdf.set_fill_color(123, 30, 30)
    pdf.rect(0, 0, 210, 42, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 24)
    pdf.set_y(8)
    pdf.cell(0, 14, "PITHAM CONSULTATION", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 8, "Payment Invoice / Bill Receipt", align="C", new_x="LMARGIN", new_y="NEXT")

    pdf.set_text_color(0, 0, 0)
    pdf.ln(12)

    # ── Invoice details row ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(95, 7, f"Invoice No: {invoice_number}", new_x="RIGHT")
    pdf.cell(95, 7, f"Date: {booked_on}", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.set_line_width(0.5)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(8)

    # ── "Bill To" section ──
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(123, 30, 30)
    pdf.cell(0, 8, "Bill To", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(2)

    bill_to = [
        ("Name", name),
        ("Email", email or "N/A"),
        ("Mobile", mobile),
    ]
    for label, value in bill_to:
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(35, 7, f"{label}:", new_x="RIGHT")
        pdf.set_font("Helvetica", "", 10)
        pdf.cell(0, 7, value, new_x="LMARGIN", new_y="NEXT")

    pdf.ln(8)

    # ── Divider ──
    pdf.set_draw_color(200, 160, 80)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(6)

    # ── Items table ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_fill_color(255, 248, 237)

    # Table header
    col_w = [15, 95, 40, 40]
    headers = ["#", "Description", "Txn Reference", "Amount"]
    for i, h in enumerate(headers):
        pdf.cell(col_w[i], 10, h, border=1, fill=True, align="C", new_x="RIGHT")
    pdf.ln()

    # Table row
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(col_w[0], 10, "1", border=1, align="C", new_x="RIGHT")
    pdf.cell(col_w[1], 10, "Spiritual Consultation", border=1, new_x="RIGHT")
    pdf.cell(col_w[2], 10, payment_reference or "N/A", border=1, align="C", new_x="RIGHT")
    pdf.cell(col_w[3], 10, f"Rs. {fee}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(2)

    # ── Total row ──
    pdf.set_font("Helvetica", "B", 11)
    total_x = col_w[0] + col_w[1] + col_w[2]
    pdf.cell(total_x, 10, "Total", border=1, align="R", new_x="RIGHT")
    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(col_w[3], 10, f"Rs. {fee}", border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(6)

    # ── Payment status ──
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(46, 125, 50)  # green
    pdf.cell(0, 8, "PAYMENT STATUS: PAID", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)

    pdf.ln(4)

    # ── Transaction details ──
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(100, 100, 100)
    if payment_reference:
        pdf.cell(0, 6, f"Transaction Reference: {payment_reference}", new_x="LMARGIN", new_y="NEXT")
    pdf.cell(0, 6, f"Booking ID: PITHAM-{appointment_id}", new_x="LMARGIN", new_y="NEXT")

    # ── Footer ──
    pdf.ln(16)
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

    # Save
    filename = f"invoice_{appointment_id}.pdf"
    filepath = os.path.join(INVOICE_DIR, filename)
    pdf.output(filepath)
    return filepath
