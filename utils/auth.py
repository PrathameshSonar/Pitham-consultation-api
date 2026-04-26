import logging
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
from sqlalchemy.orm import Session

from config import settings
from database import get_db
import models

logger = logging.getLogger("pitham.auth")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_HOURS = 24 * 7  # 7 days
COOKIE_NAME = "pitham_session"
IS_PROD = settings.core.is_production

# Fail fast if SECRET_KEY is not set in production
SECRET_KEY = settings.core.secret_key
if IS_PROD and not SECRET_KEY:
    raise RuntimeError("SECRET_KEY environment variable must be set in production!")
if not SECRET_KEY:
    SECRET_KEY = "dev-only-insecure-key-change-in-production"
    logger.warning("SECRET_KEY not set — using insecure default. Set it before deploying!")

# auto_error=False so missing-Bearer doesn't 401 immediately — we'll then try the cookie.
bearer_scheme = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    pw_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(pw_bytes, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    pw_bytes = plain.encode("utf-8")[:72]
    try:
        return bcrypt.checkpw(pw_bytes, hashed.encode("utf-8"))
    except ValueError:
        return False


def create_token(data: dict) -> str:
    payload = data.copy()
    payload["exp"] = datetime.utcnow() + timedelta(hours=ACCESS_TOKEN_EXPIRE_HOURS)
    payload["iat"] = datetime.utcnow()
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired token")


def set_auth_cookie(response: Response, token: str) -> None:
    """Set the auth token as an httpOnly cookie. Use this on login/register/google success.

    SameSite policy:
        - prod  → "none" with secure=True. Required so the cookie flows on
                  cross-site sub-resource requests (e.g. <img src="…/uploads/…"> when
                  frontend and backend live on different registrable domains).
                  CSRF protection for state-changing requests is provided by the
                  OriginCheckMiddleware in main.py rather than the cookie attribute.
        - dev   → "lax". Local frontend (localhost:3000) and backend (localhost:8000)
                  are same-site, "none" would also need secure=True which we don't
                  have over plain HTTP, and "lax" gives us CSRF defense for free.
    """
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=ACCESS_TOKEN_EXPIRE_HOURS * 3600,
        httponly=True,
        secure=IS_PROD,
        samesite="none" if IS_PROD else "lax",
        path="/",
    )


def clear_auth_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> models.User:
    # Prefer Bearer (legacy / API clients), fall back to httpOnly cookie (browser).
    token = credentials.credentials if credentials else request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(token)
    sub = payload.get("sub")
    try:
        user_id = int(sub)
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token payload")
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    return user


def require_admin(user: models.User = Depends(get_current_user)) -> models.User:
    """Require admin or moderator role."""
    if user.role not in ("admin", "moderator"):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def require_super_admin(user: models.User = Depends(get_current_user)) -> models.User:
    """Require full admin role (not moderator)."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Super admin access required")
    return user
