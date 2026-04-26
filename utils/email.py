import logging
import os
import smtplib
from concurrent.futures import ThreadPoolExecutor
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

from config import settings
from utils.whatsapp import send_whatsapp

logger = logging.getLogger("pitham.email")

# Background pool — emails are sent off-request so SMTP latency doesn't block API responses.
_email_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="email-")

BRAND = "Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar"
BRAND_SHORT = "SPBSP, Ahilyanagar"

FOOTER = f"""
<hr style="border:none;border-top:1px solid #E8D9BF;margin:24px 0">
<p style="color:#9C8573;font-size:12px">
  This is an automated message from {BRAND}.<br>
  Please do not reply to this email.
</p>
"""


def _wrap_html(body: str) -> str:
    return f"""
    <div style="font-family:'Poppins',Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
      <div style="text-align:center;margin-bottom:20px">
        <h2 style="color:#7B1E1E;margin:4px 0">{BRAND}</h2>
      </div>
      {body}
      {FOOTER}
    </div>
    """


def _send_email_sync(to: str, subject: str, html_body: str, attachments: list[str] | None):
    """Blocking SMTP send — runs in the background email pool, never on a request thread."""
    cfg = settings.email
    try:
        msg = MIMEMultipart("mixed")
        msg["Subject"] = subject
        msg["From"] = f"{BRAND} <{cfg.from_addr}>"
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

        with smtplib.SMTP(cfg.server, cfg.port) as server:
            server.starttls()
            server.login(cfg.username, cfg.password)
            server.sendmail(cfg.from_addr, to, msg.as_string())
        logger.info("EMAIL sent to=%s subject=%s", to, subject)
    except Exception as e:
        logger.error("EMAIL failed to=%s: %s", to, e)


def send_email(to: str, subject: str, html_body: str, attachments: list[str] | None = None):
    """Schedule an email send in the background. Returns immediately — never blocks the caller."""
    if not to:
        return
    if not settings.email.is_configured():
        logger.info("EMAIL (stub) to=%s subject=%s", to, subject)
        return
    _email_pool.submit(_send_email_sync, to, subject, html_body, attachments)


# ── Email Templates ──────────────────────────────────────────────────────────

def send_booking_confirmation(to: str, name: str, booking_id: int, fee: str, receipt_path: str = "", mobile: str = ""):
    """Sent after payment is verified. Attaches receipt PDF if available. Also fires WhatsApp if `mobile` is supplied."""
    subject = f"Booking Confirmed — {BRAND}"
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation booking <strong>SPBSP-{booking_id}</strong> has been confirmed.</p>
    <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
      <p style="margin:0"><strong>Consultation Fee:</strong> ₹{fee}</p>
      <p style="margin:4px 0 0"><strong>Payment Status:</strong> <span style="color:#2E7D32">Paid ✓</span></p>
    </div>
    <p>Our team will review your request and schedule your consultation shortly. You will receive another email with the date, time, and Zoom link.</p>
    <p>If you have any questions, please use the Queries section in your dashboard.</p>
    <p style="margin-top:24px">Regards,<br><strong>Shri Mayuresh Guruji Vispute</strong><br>{BRAND}</p>
    """)
    attachments = [receipt_path] if receipt_path else []
    send_email(to, subject, body, attachments)
    send_whatsapp(
        mobile,
        f"🙏 Namaste {name}, your {BRAND_SHORT} consultation booking SPBSP-{booking_id} is confirmed. "
        f"Fee ₹{fee} paid. We'll share the date, time, and Zoom link shortly.",
    )


def send_appointment_confirmation(to: str, name: str, scheduled_date: str, scheduled_time: str, zoom_link: str, mobile: str = "", appointment_id: int | None = None):
    """Sent when admin assigns a time slot. Also fires WhatsApp if `mobile` is supplied.
    Attaches an .ics calendar invite so the user can add the event to their calendar
    in one tap from Gmail / Apple Mail / Outlook."""
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
    <p>A calendar invite (.ics) is attached — open it on your phone to add the meeting to your calendar.</p>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)

    attachments: list[str] = []
    if appointment_id is not None:
        try:
            from utils.ics import generate_ics
            ics_path = generate_ics(
                appointment_id=appointment_id,
                summary=f"Consultation with Shri Mayuresh Guruji ({BRAND_SHORT})",
                description=f"Online consultation via Zoom.\nMeeting link: {zoom_link}",
                location=zoom_link or "Zoom",
                scheduled_date=scheduled_date,
                scheduled_time=scheduled_time,
                attendee_email=to,
            )
            if ics_path:
                attachments.append(ics_path)
        except Exception as e:
            logger.warning("ics generation failed for appt=%s: %s", appointment_id, e)

    send_email(to, subject, body, attachments)
    send_whatsapp(
        mobile,
        f"🙏 {name}, your consultation with Guruji is scheduled for {scheduled_date} at {scheduled_time}.\n"
        f"Join 5 min early: {zoom_link}\n— {BRAND_SHORT}",
    )


def send_reschedule_notification(to: str, name: str, scheduled_date: str, scheduled_time: str, zoom_link: str, reason: str = "", mobile: str = ""):
    """Sent when admin reschedules. Also fires WhatsApp if `mobile` is supplied."""
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
    reason_suffix = f"\nReason: {reason}" if reason else ""
    send_whatsapp(
        mobile,
        f"🙏 {name}, your consultation has been rescheduled to {scheduled_date} at {scheduled_time}.{reason_suffix}\n"
        f"Zoom: {zoom_link}\n— {BRAND_SHORT}",
    )


def send_completion_notification(to: str, name: str, booking_id: int, analysis_path: str = "", recording_link: str = "", mobile: str = ""):
    """Sent when consultation is marked completed. Also fires WhatsApp if `mobile` is supplied."""
    subject = f"Consultation Completed — {BRAND}"
    extras = ""
    if analysis_path:
        extras += "<p>✅ <strong>Consultation analysis</strong> has been uploaded to your dashboard.</p>"
    if recording_link:
        extras += f'<p>🎥 <strong>Recording:</strong> <a href="{recording_link}" style="color:#E65100">Watch Recording</a></p>'
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your consultation <strong>SPBSP-{booking_id}</strong> has been completed.</p>
    {extras}
    <p>Please check your dashboard for:</p>
    <ul>
      <li>Consultation analysis document</li>
      <li>Sadhna documents (if assigned)</li>
      <li>Session recording (if available)</li>
    </ul>
    <p>If you need any follow-up, please book a new consultation or raise a query.</p>
    <p style="margin-top:24px">Regards,<br><strong>Shri Mayuresh Guruji Vispute</strong><br>{BRAND}</p>
    """)
    send_email(to, subject, body)
    wa_lines = [f"🙏 {name}, your consultation SPBSP-{booking_id} is complete."]
    if analysis_path:
        wa_lines.append("✅ Analysis uploaded to your dashboard.")
    if recording_link:
        wa_lines.append(f"🎥 Recording: {recording_link}")
    wa_lines.append(f"— {BRAND_SHORT}")
    send_whatsapp(mobile, "\n".join(wa_lines))


# ── OTP helper — send a password reset code over both channels ───────────────

def send_event_registration_confirmation(
    to: str,
    name: str,
    event_title: str,
    event_date: str,
    event_time: str = "",
    location: str = "",
    fee_amount: int = 0,
    payment_status: str = "n/a",
    custom_message: str = "",
    mobile: str = "",
):
    """Sent after a successful event registration. Free events: confirmation
    is immediate. Paid events: only sent once the gateway confirms payment.

    `payment_status` is one of "paid" / "pending" / "n/a" — drives the
    coloured pill at the top of the email."""
    subject = f"Registered: {event_title} — {BRAND}"

    if payment_status == "paid":
        pay_pill = '<span style="color:#2E7D32">Paid ✓</span>'
    elif payment_status == "pending":
        pay_pill = '<span style="color:#E65100">Pending — admin will verify</span>'
    else:
        pay_pill = '<span style="color:#2E7D32">No payment required</span>'

    when = event_date + (f" at {event_time}" if event_time else "")
    location_row = (
        f'<tr><td style="padding:4px 16px 4px 0"><strong>Location:</strong></td><td>{location}</td></tr>'
        if location else ""
    )
    fee_row = (
        f'<tr><td style="padding:4px 16px 4px 0"><strong>Fee:</strong></td><td>₹{fee_amount} — {pay_pill}</td></tr>'
        if fee_amount > 0 else
        f'<tr><td style="padding:4px 16px 4px 0"><strong>Fee:</strong></td><td>{pay_pill}</td></tr>'
    )
    custom_html = (
        f'<div style="background:#F5E6C8;padding:12px 16px;border-radius:8px;margin:12px 0">{custom_message}</div>'
        if custom_message else ""
    )

    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>Your registration for <strong>{event_title}</strong> is confirmed.</p>
    <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
      <table style="border-collapse:collapse">
        <tr><td style="padding:4px 16px 4px 0"><strong>Event:</strong></td><td>{event_title}</td></tr>
        <tr><td style="padding:4px 16px 4px 0"><strong>When:</strong></td><td>{when}</td></tr>
        {location_row}
        {fee_row}
      </table>
    </div>
    {custom_html}
    <p>You can view all your registrations under <strong>My Events</strong> in your dashboard.</p>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to, subject, body)
    pay_line = f" Fee ₹{fee_amount} ({payment_status})." if fee_amount > 0 else ""
    send_whatsapp(
        mobile,
        f"🙏 {name}, your registration for '{event_title}' on {when} is confirmed.{pay_line}\n— {BRAND_SHORT}",
    )


def send_event_waitlist_added(
    to: str,
    name: str,
    event_title: str,
    event_date: str,
    mobile: str = "",
):
    """Sent when a user joins the waitlist (event was full at signup time)."""
    subject = f"You're on the waitlist: {event_title} — {BRAND}"
    body = _wrap_html(f"""
    <h3>Namaste {name},</h3>
    <p>The event <strong>{event_title}</strong> on {event_date} is currently full,
       so we've added you to the <strong>waitlist</strong>.</p>
    <p>If a spot opens up, we'll email you with instructions to confirm your registration
       (and pay, if applicable).</p>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to, subject, body)
    send_whatsapp(
        mobile,
        f"🙏 {name}, '{event_title}' on {event_date} is full. You're on the waitlist — "
        f"we'll let you know if a spot opens. — {BRAND_SHORT}",
    )


def send_event_waitlist_promoted(
    to: str,
    name: str,
    event_title: str,
    event_date: str,
    fee_amount: int,
    needs_payment: bool,
    event_url: str,
    mobile: str = "",
):
    """Sent when admin/cancellation moves a waitlisted user up. For free
    events, the registration flips straight to confirmed; for paid events
    the user must come back and complete payment."""
    subject = f"A spot opened — {event_title} — {BRAND}"
    cta_html = (
        f'<p style="text-align:center;margin:24px 0">'
        f'<a href="{event_url}" style="background:#E65100;color:#fff;padding:12px 32px;'
        f'border-radius:999px;text-decoration:none;font-weight:600">Complete payment</a></p>'
        if needs_payment else ""
    )
    msg = (
        f"You can now confirm your spot. Fee ₹{fee_amount} — please complete payment within 48 hours."
        if needs_payment
        else "Your spot is confirmed. See you at the event!"
    )
    body = _wrap_html(f"""
    <h3>Good news, {name}!</h3>
    <p>A spot has opened up for <strong>{event_title}</strong> on {event_date}.</p>
    <p>{msg}</p>
    {cta_html}
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to, subject, body)
    wa_msg = (
        f"🎉 {name}, a spot opened for '{event_title}' on {event_date}. "
        + (f"Complete payment of ₹{fee_amount}: {event_url}" if needs_payment else "Your spot is confirmed.")
    )
    send_whatsapp(mobile, wa_msg + f"\n— {BRAND_SHORT}")


def send_password_reset_otp(to_email: str, to_mobile: str, name: str, otp: str):
    """Send the 6-digit reset OTP via email AND WhatsApp (whichever is configured).
    Callers can leave either recipient blank."""
    subject = f"Password Reset OTP — {BRAND_SHORT}"
    body = _wrap_html(f"""
    <h3>Namaste {name or "Devotee"},</h3>
    <p>Use the 6-digit OTP below to reset your password. This OTP is valid for <strong>10 minutes</strong>.</p>
    <div style="background:#FFF4DE;padding:20px;border-radius:8px;margin:16px 0;text-align:center">
      <code style="font-size:2rem;font-weight:bold;color:#7B1E1E;letter-spacing:0.4em">{otp}</code>
    </div>
    <p>If you did not request this reset, please ignore this message.</p>
    <p style="margin-top:24px">Regards,<br><strong>{BRAND}</strong></p>
    """)
    send_email(to_email, subject, body)
    send_whatsapp(
        to_mobile,
        f"🔐 Your {BRAND_SHORT} password reset OTP is *{otp}*. Valid for 10 minutes. If you did not request this, please ignore.",
    )
