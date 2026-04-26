from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from database import get_db
import models
import schemas
from utils.auth import get_current_user, require_admin
from utils.permissions import require_section

_section_admin = require_section("queries")

router = APIRouter(tags=["queries"])


# ── User: ask a query ─────────────────────────────────────────────────────────

@router.post("/queries", response_model=schemas.QueryOut)
def create_query(
    data: schemas.QueryCreate,
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = models.Query(
        user_id=user.id,
        subject=data.subject,
        message=data.message,
    )
    db.add(query)
    db.commit()
    db.refresh(query)
    return query


# ── User: my queries ──────────────────────────────────────────────────────────

@router.get("/queries/my", response_model=List[schemas.QueryOut])
def my_queries(
    user: models.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    return (
        db.query(models.Query)
        .filter(models.Query.user_id == user.id)
        .order_by(models.Query.created_at.desc())
        .all()
    )


# ── Admin: all queries ────────────────────────────────────────────────────────

@router.get("/admin/queries", response_model=List[schemas.QueryOut])
def admin_all_queries(
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    return db.query(models.Query).order_by(models.Query.created_at.desc()).all()


# ── Admin: reply to query ─────────────────────────────────────────────────────

@router.put("/admin/queries/{query_id}/reply", response_model=schemas.QueryOut)
def reply_to_query(
    query_id: int,
    data: schemas.QueryReply,
    admin: models.User = Depends(_section_admin),
    db: Session = Depends(get_db),
):
    query = db.query(models.Query).filter(models.Query.id == query_id).first()
    if not query:
        raise HTTPException(status_code=404, detail="Query not found")

    query.reply = data.reply
    query.replied_at = datetime.utcnow()
    query.status = "answered"
    db.commit()
    db.refresh(query)
    return query
