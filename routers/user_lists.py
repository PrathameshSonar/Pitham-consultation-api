from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List

from database import get_db
import models
import schemas
from utils.auth import require_admin
from utils.audit import log_action

router = APIRouter(prefix="/admin/user-lists", tags=["user-lists"])


def _to_out(ul: models.UserList) -> dict:
    return {
        "id": ul.id,
        "name": ul.name,
        "description": ul.description,
        "created_at": ul.created_at,
        "member_count": len(ul.members),
        "member_ids": [m.user_id for m in ul.members],
    }


@router.post("", response_model=schemas.UserListOut)
def create_list(
    data: schemas.UserListCreate,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ul = models.UserList(
        name=data.name,
        description=data.description,
        created_by=admin.id,
    )
    db.add(ul)
    db.flush()

    for uid in set(data.user_ids):
        db.add(models.UserListMember(user_list_id=ul.id, user_id=uid))

    db.commit()
    db.refresh(ul)
    log_action(db, admin.id, "create_list", "user_list", ul.id, f"'{data.name}' with {len(data.user_ids)} members")
    return _to_out(ul)


@router.get("", response_model=List[schemas.UserListOut])
def list_lists(
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    lists = db.query(models.UserList).order_by(models.UserList.created_at.desc()).all()
    return [_to_out(ul) for ul in lists]


@router.patch("/{list_id}", response_model=schemas.UserListOut)
def update_list(
    list_id: int,
    data: schemas.UserListUpdate,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ul = db.query(models.UserList).filter(models.UserList.id == list_id).first()
    if not ul:
        raise HTTPException(status_code=404, detail="List not found")
    if data.name is not None:
        ul.name = data.name
    if data.description is not None:
        ul.description = data.description
    db.commit()
    db.refresh(ul)
    return _to_out(ul)


@router.put("/{list_id}/members", response_model=schemas.UserListOut)
def update_members(
    list_id: int,
    data: schemas.UserListMembersUpdate,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ul = db.query(models.UserList).filter(models.UserList.id == list_id).first()
    if not ul:
        raise HTTPException(status_code=404, detail="List not found")

    # Replace membership.
    db.query(models.UserListMember).filter(
        models.UserListMember.user_list_id == list_id
    ).delete()
    for uid in set(data.user_ids):
        db.add(models.UserListMember(user_list_id=list_id, user_id=uid))

    db.commit()
    db.refresh(ul)
    return _to_out(ul)


@router.delete("/{list_id}")
def delete_list(
    list_id: int,
    admin: models.User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ul = db.query(models.UserList).filter(models.UserList.id == list_id).first()
    if not ul:
        raise HTTPException(status_code=404, detail="List not found")
    name = ul.name
    db.delete(ul)
    db.commit()
    log_action(db, admin.id, "delete_list", "user_list", list_id, f"'{name}'")
    return {"message": "Deleted"}
