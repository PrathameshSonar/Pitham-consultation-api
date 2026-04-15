import logging
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
logger = logging.getLogger("pitham.email")

MAIL_USERNAME = os.getenv("MAIL_USERNAME", "")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD", "")
MAIL_FROM = os.getenv("MAIL_FROM", "") or MAIL_USERNAME
MAIL_SERVER = os.getenv("MAIL_SERVER", "smtp.gmail.com")
MAIL_PORT = int(os.getenv("MAIL_PORT", "587"))

BRAND = "Pitham Consultation"

FOOTER = """
<hr style="border:none;border-top:1px solid #E8D9BF;margin:24px 0">
<p style="color:#9C8573;font-size:12px">
  This is an automated message from Pitham Consultation.<br>
  Please do not reply to this email.
</p>
"""


def _wrap_html(body: str) -> str:
    return f"""
    <div style="font-family:'Poppins',Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
      <div style="text-align:center;margin-bottom:20px">
        <span style="font-size:2rem;color:#E65100">ॐ</span>
        <h2 style="color:#7B1E1E;margin:4px 0">{BRAND}</h2>
      </div>
      {body}
      {FOOTER}
    </div>
    """


def send_email(to: str, subject: str, html_body: str, attachments: list[str] | None = None):
    """Send email with optional file attachments. Never crashes the caller."""
    if not to:
        return
    if not MAIL_USERNAME or not MAIL_PASSWORD:
        logger.info("EMAIL (stub) to=%s subject=%s", to, subject)
        return

    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"{BRAND} <{MAIL_FROM}>"
        msg["To"] = to

        html_part = MIMEMultipart("alternative")
        html_part.attach(MIMEText(html_body, "html"))
        msg.attach(html_part)

        for filepath in (attachments or []):
            if filepath and os.path.exists(filepath):
                with open(filepath, "rb") as f:
                    part = MIMEApplication(f.read(), Name=os.path.basename(filepath))
                    part["Content-Disposition"] = f'attachment; filename="{os.path.basename(filepath)}"'
                    msg.attach(part)

        with smtplib.SMTP(MAIL_SERVER, MAIL_PORT) as server:
            server.starttls()
            server.login(MAIL_USERNAME, MAIL_PASSWORD)
            server.sendmail(MAIL_FROM, to, msg.as_string())
        logger.info("EMAIL sent to=%s subject=%s", to, subject)
    except Exception as e:
        logger.error("EMAIL failed to=%s: %s", to, e)


# ── Email Templates ──────────────────────────────────────────────────────────

def send_booking_confirmation(to: str, name: str, booking_id: int, fee: str, receipt_path: str = ""):
    """Sent after payment is verified. Attaches receipt PDF if available."""
    subject = f"Booking Confirmed — {BRAND}"
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation booking <strong>PITHAM-{booking_id}</strong> has been confirmed.</p>
    <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
      <p style="margin:0"><strong>Consultation Fee:</strong> ₹{fee}</p>
      <p style="margin:4px 0 0"><strong>Payment Status:</strong> <span style="color:#2E7D32">Paid ✓</span></p>
    </div>
    <p>Our team will review your request and schedule your consultation shortly. You will receive another email with the date, time, and Zoom link.</p>
    <p>If you have any questions, please use the Queries section in your dashboard.</p>
    <p style="margin-top:24px">Regards,<br><strong>Shri Mayuresh Vispute Guruji</strong><br>{BRAND}</p>
    """)
    attachments = [receipt_path] if receipt_path else []
    send_email(to, subject, body, attachments)


def send_appointment_confirmation(to: str, name: str, scheduled_date: str, scheduled_time: str, zoom_link: str):
    """Sent when admin assigns a time slot."""
    subject = f"Consultation Scheduled — {BRAND}"
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation with Guruji has been scheduled.</p>
    <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
      <table style="border-collapse:collapse">
        <tr><td style="padding:4px 16px 4px 0"><strong>Date:</strong></td><td>{scheduled_date}</td></tr>
        <tr><td style="padding:4px 16px 4px 0"><strong>Time:</strong></td><td>{scheduled_time}</td></tr>
        <tr><td style="padding:4px 16px 4px 0"><strong>Zoom:</strong></td><td><a href="{zoom_link}" style="color:#E65100">{zoom_link}</a></td></tr>
      </table>
    </div>
    <p>Please join the Zoom meeting <strong>5 minutes before</strong> the scheduled time.</p>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to, subject, body)


def send_reschedule_notification(to: str, name: str, scheduled_date: str, scheduled_time: str, zoom_link: str, reason: str = ""):
    """Sent when admin reschedules."""
    subject = f"Consultation Rescheduled — {BRAND}"
    reason_text = f"<p><strong>Reason:</strong> {reason}</p>" if reason else ""
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation has been rescheduled.</p>
    {reason_text}
    <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
      <table style="border-collapse:collapse">
        <tr><td style="padding:4px 16px 4px 0"><strong>New Date:</strong></td><td>{scheduled_date}</td></tr>
        <tr><td style="padding:4px 16px 4px 0"><strong>New Time:</strong></td><td>{scheduled_time}</td></tr>
        <tr><td style="padding:4px 16px 4px 0"><strong>Zoom:</strong></td><td><a href="{zoom_link}" style="color:#E65100">{zoom_link}</a></td></tr>
      </table>
    </div>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to, subject, body)


def send_completion_notification(to: str, name: str, booking_id: int, analysis_path: str = "", recording_link: str = ""):
    """Sent when consultation is marked completed."""
    subject = f"Consultation Completed — {BRAND}"
    extras = ""
    if analysis_path:
        extras += "<p>✅ <strong>Consultation analysis</strong> has been uploaded to your dashboard.</p>"
    if recording_link:
        extras += f'<p>🎥 <strong>Recording:</strong> <a href="{recording_link}" style="color:#E65100">Watch Recording</a></p>'
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation <strong>PITHAM-{booking_id}</strong> has been completed.</p>
    {extras}
    <p>Please check your dashboard for:</p>
    <ul>
      <li>Consultation analysis document</li>
      <li>Sadhna documents (if assigned)</li>
      <li>Session recording (if available)</li>
    </ul>
    <p>If you need any follow-up, please book a new consultation or raise a query.</p>
    <p style="margin-top:24px">Regards,<br><strong>Shri Mayuresh Vispute Guruji</strong><br>{BRAND}</p>
    """)
    send_email(to, subject, body)
