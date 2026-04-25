"""Single source of truth for admin-configurable settings.

Why this exists: routers used to inline `db.query(SiteSetting).filter(...).first()`
and fall back to a hardcoded "500" if the row was missing. That meant a fresh
database (where the admin hadn't saved settings yet) showed "500" in receipts /
emails / invoices instead of the real default of "3500".

Now every caller goes through `get_setting(db, key)` which knows the proper
defaults via the DEFAULTS map. Update DEFAULTS here and the change propagates
everywhere automatically.
"""

from sqlalchemy.orm import Session

import models


DEFAULT_TERMS = """<h3>Consultation Terms &amp; Conditions</h3>
<ol>
<li><strong>Services:</strong> Shri Pitambara Baglamukhi Shakti Pitham, Ahilyanagar (SPBSP) provides astrology and spiritual consultation by Shri Mayuresh Guruji Vispute via Zoom.</li>
<li><strong>Payment:</strong> Full payment is required before scheduling. Payments are non-refundable once the session is confirmed.</li>
<li><strong>Privacy:</strong> Your personal information and consultation records are kept strictly confidential.</li>
<li><strong>Rescheduling:</strong> If Guruji needs to reschedule, it will be done at no extra cost. For user-initiated rescheduling, raise a query.</li>
<li><strong>Disclaimer:</strong> Astrological guidance is for spiritual and informational purposes only. It does not constitute medical, legal, or financial advice.</li>
<li><strong>Conduct:</strong> Users must be respectful during consultations. Inappropriate behaviour may lead to session cancellation without refund.</li>
</ol>"""


DEFAULTS: dict[str, str] = {
    "consultation_fee": "3500",
    "booking_enabled": "true",
    "booking_resume_date": "",
    "booking_hold_message": "",
    "booking_limit": "0",
    "booking_limit_deadline": "",
    "consultation_terms": DEFAULT_TERMS,
    # Social links
    "social_facebook": "",
    "social_instagram": "",
    "social_youtube": "",
    "social_twitter": "",
    "social_whatsapp": "",
    # Contact info
    "contact_email": "",
    "contact_phone": "",
    "contact_address": "",
    "contact_map_url": "",
}


def get_setting(db: Session, key: str) -> str:
    """Return the stored value or the default. Use this everywhere — never
    inline a `db.query(SiteSetting).first()` with your own fallback string."""
    row = db.query(models.SiteSetting).filter(models.SiteSetting.key == key).first()
    if row and row.value is not None:
        return row.value
    return DEFAULTS.get(key, "")


def set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(models.SiteSetting).filter(models.SiteSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(models.SiteSetting(key=key, value=value))
    db.commit()


def get_consultation_fee(db: Session) -> int:
    """Convenience: parse the consultation_fee setting as an int."""
    raw = get_setting(db, "consultation_fee") or DEFAULTS["consultation_fee"]
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(DEFAULTS["consultation_fee"])
