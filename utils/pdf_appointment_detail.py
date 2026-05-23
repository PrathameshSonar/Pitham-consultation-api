"""Server-side PDF for the admin "Download Details" action.


Admin needs a self-contained PDF they can share with Guruji ahead of the
consultation. Layout:


  Page 1 — Booking summary (name/DOB/TOB/birth place, problem statement).
  Page 2 — User's selfie, full-bleed.
  Page 3+ — Analysis notes + each analysis file. Image files (jpg/png) are
            embedded inline; non-image files (pdf/docx) are listed as a
            link line that resolves on the recipient's end.


This module ONLY composes the PDF — ownership / authorisation checks live
in the router that calls it.
"""


from __future__ import annotations


import io
import os
from typing import Iterable


from fpdf import FPDF




_MAROON = (123, 30, 30)
_SAFFRON = (200, 160, 80)
_IMG_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}




def _header(pdf: FPDF, subtitle: str) -> None:
    """Maroon banner with the institute name + a one-line subtitle. Called
    once per logical section so the second / third pages still feel like
    the same document."""
    pdf.set_fill_color(*_MAROON)
    pdf.rect(0, 0, 210, 32, "F")
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_y(6)
    pdf.cell(0, 7, "SHRI PITAMBARA BAGLAMUKHI SHAKTI PITHAM, AHILYANAGAR", align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, subtitle, align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(8)




def _section_title(pdf: FPDF, title: str) -> None:
    pdf.set_font("Helvetica", "B", 13)
    pdf.set_text_color(*_MAROON)
    pdf.cell(0, 8, title, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(0, 0, 0)
    pdf.set_draw_color(*_SAFFRON)
    pdf.line(10, pdf.get_y(), 200, pdf.get_y())
    pdf.ln(3)




def _detail_row(pdf: FPDF, label: str, value: str) -> None:
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(45, 6, f"{label}:", new_x="RIGHT")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, value or "—")




def _maybe_embed_image(pdf: FPDF, path: str, *, max_height: float = 200) -> bool:
    """Try to embed `path` as an image. Returns False if the file isn't a
    supported image type or can't be opened — caller can fall back to a
    text link line."""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in _IMG_EXTS:
        return False
    try:
        # Width 180mm leaves a 15mm margin on each side; height auto-scales
        # to preserve aspect. fpdf2 reads dimensions from the file directly.
        pdf.image(path, x=15, w=180, h=max_height, keep_aspect_ratio=True)
        return True
    except Exception:
        return False




def build_appointment_detail_pdf(
    *,
    appointment_id: int,
    name: str,
    email: str,
    mobile: str,
    dob: str,
    tob: str,
    birth_place: str,
    problem: str,
    booked_on: str,
    scheduled_date: str | None,
    scheduled_time: str | None,
    status: str,
    selfie_path: str | None,
    analysis_notes: str | None,
    analysis_paths: Iterable[str],
) -> bytes:
    """Compose the PDF and return its bytes. Caller owns streaming /
    writing to disk."""
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)


    # ── Page 1 — booking summary ──────────────────────────────────────
    pdf.add_page()
    _header(pdf, f"Consultation Details — SPBSP-{appointment_id}")


    _section_title(pdf, "Consultee")
    _detail_row(pdf, "Name", name)
    _detail_row(pdf, "Email", email or "—")
    _detail_row(pdf, "Mobile", mobile or "—")
    _detail_row(pdf, "Date of Birth", dob or "—")
    _detail_row(pdf, "Time of Birth", tob or "—")
    _detail_row(pdf, "Birth Place", birth_place or "—")
    pdf.ln(4)


    _section_title(pdf, "Booking")
    _detail_row(pdf, "Booking ID", f"SPBSP-{appointment_id}")
    _detail_row(pdf, "Booked on", booked_on or "—")
    _detail_row(pdf, "Status", (status or "—").replace("_", " "))
    if scheduled_date:
        when = scheduled_date
        if scheduled_time:
            when += f" · {scheduled_time}"
        _detail_row(pdf, "Scheduled", when)
    pdf.ln(4)


    _section_title(pdf, "Problem Statement")
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, problem or "—")


    # ── Page 2 — selfie ───────────────────────────────────────────────
    if selfie_path and os.path.isfile(selfie_path):
        pdf.add_page()
        _header(pdf, f"Consultee selfie — SPBSP-{appointment_id}")
        # The selfie is the whole point of this page; centre it vertically
        # in the remaining space by leaving the y cursor where the header
        # left it and capping height.
        if not _maybe_embed_image(pdf, selfie_path, max_height=220):
            pdf.set_font("Helvetica", "I", 10)
            pdf.cell(0, 6, "(Selfie file could not be embedded.)", new_x="LMARGIN", new_y="NEXT")


    # ── Page 3+ — analysis ────────────────────────────────────────────
    paths = [p for p in (analysis_paths or []) if p]
    if analysis_notes or paths:
        pdf.add_page()
        _header(pdf, f"Analysis — SPBSP-{appointment_id}")


        if analysis_notes:
            _section_title(pdf, "Guruji's notes")
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 6, analysis_notes)
            pdf.ln(4)


        if paths:
            _section_title(pdf, "Attachments")
            for idx, p in enumerate(paths, start=1):
                pdf.set_font("Helvetica", "B", 10)
                pdf.cell(0, 6, f"#{idx} — {os.path.basename(p)}", new_x="LMARGIN", new_y="NEXT")
                # Embed images inline; non-image files just get listed.
                if not _maybe_embed_image(pdf, p, max_height=140):
                    pdf.set_font("Helvetica", "I", 9)
                    pdf.cell(0, 5, "(Non-image file — open the original via the dashboard.)", new_x="LMARGIN", new_y="NEXT")
                pdf.ln(4)
                # Each image takes meaningful space; new page if we're
                # near the bottom of the current one.
                if pdf.get_y() > 240:
                    pdf.add_page()
                    _header(pdf, f"Analysis (cont.) — SPBSP-{appointment_id}")


    # fpdf2 emits a bytearray; the StreamingResponse caller needs bytes.
    buf = io.BytesIO()
    pdf.output(buf)
    return buf.getvalue()
