"""Audit log helper — call after any admin action."""

import json
import logging
from sqlalchemy.orm import Session
import models

logger = logging.getLogger("pitham.audit")


def log_action(
    db: Session,
    admin_id: int,
    action: str,
    entity_type: str = "",
    entity_id: int = 0,
    details: str = "",
):
    entry = models.AuditLog(
        admin_id=admin_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    db.add(entry)
    db.commit()
    logger.info("AUDIT | admin=%s action=%s %s #%s | %s", admin_id, action, entity_type, entity_id, details)
