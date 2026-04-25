"""
Automated appointment reminder scheduler.

Strategy:
- A single APScheduler BackgroundScheduler runs in-process and ticks every
  5 minutes. Each tick scans for scheduled appointments whose time falls in
  the next-24h or next-1h windows, and sends an email + WhatsApp reminder
  to anyone we haven't reminded yet for that window.
- Sent state is tracked on the appointment row (`reminder_24h_sent_at`,
  `reminder_1h_sent_at`) so a process restart never causes a duplicate
  reminder.
- Times stored on the appointment are IST (Asia/Kolkata).
- Best-effort: any exception is logged and swallowed. Reminder failures must
  never crash the scheduler thread.
"""

import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.background import BackgroundScheduler

from database import SessionLocal
import models
from utils.email import send_email
from utils.whatsapp import send_whatsapp

logger = logging.getLogger("pitham.reminders")

# Singleton — start once, share across the process
_scheduler: BackgroundScheduler | None = None

BRAND = "Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar"
BRAND_SHORT = "SPBSP, Ahilyanagar"


def _appt_datetime(appt: models.Appointment) -> datetime | None:
    """Parse the IST date+time stored on the appointment into a naive datetime.
    Treated as IST throughout — comparing to datetime.now(IST) keeps it consistent."""
    if not appt.scheduled_date or not appt.scheduled_time:
        return None
    try:
        time_str = appt.scheduled_time if len(appt.scheduled_time) == 8 else f"{appt.scheduled_time}:00"
        return datetime.strptime(f"{appt.scheduled_date} {time_str}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _send_reminder(appt: models.Appointment, label: str):
    """Send the actual reminder via email + WhatsApp."""
    user = appt.user
    if not user:
        return
    when = f"{appt.scheduled_date} at {appt.scheduled_time}"
    subject = f"Reminder: Your consultation is {label} — {BRAND}"
    body = f"""
    <div style="font-family:'Poppins',Arial,sans-serif;max-width:600px;margin:0 auto;padding:20px;color:#3D2817">
      <h2 style="color:#7B1E1E">{BRAND}</h2>
      <h3>Namaste {user.name},</h3>
      <p>This is a friendly reminder that your consultation with Guruji is <strong>{label}</strong>.</p>
      <div style="background:#FFF4DE;padding:16px;border-radius:8px;margin:16px 0">
        <p style="margin:0"><strong>When:</strong> {when} (IST)</p>
        {f'<p style="margin:4px 0 0"><strong>Zoom:</strong> <a href="{appt.zoom_link}" style="color:#E65100">{appt.zoom_link}</a></p>' if appt.zoom_link else ''}
      </div>
      <p>Please join 5 minutes before the scheduled time.</p>
      <p style="color:#9C8573;font-size:12px;margin-top:24px">— {BRAND}</p>
    </div>
    """
    if user.email:
        send_email(user.email, subject, body)
    if user.mobile:
        zoom_part = f"\nJoin: {appt.zoom_link}" if appt.zoom_link else ""
        send_whatsapp(
            user.mobile,
            f"🙏 {user.name}, reminder: your consultation with Guruji is {label} on {when} (IST).{zoom_part}\n— {BRAND_SHORT}",
        )


def _check_and_send_reminders():
    """One scheduler tick. Idempotent — safe to run on every fire."""
    db = SessionLocal()
    try:
        now = datetime.utcnow() + timedelta(hours=5, minutes=30)  # naive IST
        # Pull a small upcoming window. Any appt in next 25h is a candidate
        # for either reminder; finer filtering happens below.
        in_25h = now + timedelta(hours=25)
        candidates = (
            db.query(models.Appointment)
            .filter(
                models.Appointment.status.in_(["scheduled", "rescheduled"]),
                models.Appointment.scheduled_date.isnot(None),
                models.Appointment.scheduled_time.isnot(None),
            )
            .all()
        )

        sent_24h = 0
        sent_1h = 0
        for appt in candidates:
            when = _appt_datetime(appt)
            if not when or when <= now or when > in_25h:
                continue

            delta_minutes = (when - now).total_seconds() / 60.0

            # 24h reminder window: between 23h and 24h30m before
            if (
                23 * 60 <= delta_minutes <= 24 * 60 + 30
                and appt.reminder_24h_sent_at is None
            ):
                try:
                    _send_reminder(appt, "tomorrow")
                    appt.reminder_24h_sent_at = datetime.utcnow()
                    db.commit()
                    sent_24h += 1
                except Exception as e:
                    logger.error("24h reminder failed appt=%s: %s", appt.id, e)
                    db.rollback()

            # 1h reminder window: between 50min and 70min before
            if (
                50 <= delta_minutes <= 70
                and appt.reminder_1h_sent_at is None
            ):
                try:
                    _send_reminder(appt, "in about 1 hour")
                    appt.reminder_1h_sent_at = datetime.utcnow()
                    db.commit()
                    sent_1h += 1
                except Exception as e:
                    logger.error("1h reminder failed appt=%s: %s", appt.id, e)
                    db.rollback()

        if sent_24h or sent_1h:
            logger.info("Reminders sent: 24h=%d, 1h=%d", sent_24h, sent_1h)
    except Exception as e:
        logger.error("Reminder tick crashed: %s", e)
    finally:
        db.close()


def start_reminder_scheduler():
    """Idempotent — safe to call multiple times (e.g. with --reload)."""
    global _scheduler
    if _scheduler is not None:
        return
    try:
        sched = BackgroundScheduler(timezone="Asia/Kolkata")
        sched.add_job(
            _check_and_send_reminders,
            "interval",
            minutes=5,
            id="appt_reminders",
            replace_existing=True,
            coalesce=True,
            max_instances=1,
        )
        sched.start()
        _scheduler = sched
        logger.info("Appointment reminder scheduler started (5-min tick).")
    except Exception as e:
        logger.error("Failed to start reminder scheduler: %s", e)
