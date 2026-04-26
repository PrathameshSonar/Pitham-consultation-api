import logging
import os

# config.py handles load_dotenv() and exposes every credential via a typed settings object
from config import settings

# ── Sentry — initialise BEFORE FastAPI so unhandled errors during init are captured ──
if settings.sentry.is_configured():
    try:
        import sentry_sdk
        sentry_sdk.init(
            dsn=settings.sentry.dsn,
            environment=settings.core.env,
            traces_sample_rate=settings.sentry.traces_rate,
            send_default_pii=False,  # don't auto-send user IPs / cookies
        )
    except Exception:
        # Never let observability take down the app
        pass

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from sqlalchemy import inspect, text

from database import engine, Base
import models  # noqa: F401 — registers all models with Base
from routers import (
    auth, appointments, users, documents, queries,
    recordings, user_lists, payments, analytics, admin_tools,
    events, pitham, broadcasts, feedback, files,
)
from routers import settings as settings_router  # renamed to avoid collision with config.settings

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pitham")

# ── DB tables ─────────────────────────────────────────────────────────────────
Base.metadata.create_all(bind=engine)


def _ensure_column(table: str, column: str, ddl: str):
    """Add a column to an existing table if missing (idempotent micro-migration).

    create_all() only creates missing tables — it never adds columns to existing
    ones. Never raise from here — a failed migration should never block startup.
    """
    try:
        insp = inspect(engine)
        if table not in insp.get_table_names():
            return  # create_all will handle it when the table is first created
        existing = {c["name"] for c in insp.get_columns(table)}
        if column in existing:
            return
        with engine.begin() as conn:
            conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl}"))
        logger.info("Migration: added column %s.%s", table, column)
    except Exception as e:
        logger.error("Migration failed for %s.%s: %s", table, column, e)


# Micro-migrations — keep idempotent
_ensure_column("events", "is_featured", "is_featured BOOLEAN NOT NULL DEFAULT 0")
_ensure_column("events", "location_map_url", "location_map_url VARCHAR(500) NULL")
_ensure_column("appointments", "reminder_24h_sent_at", "reminder_24h_sent_at TIMESTAMP NULL")
_ensure_column("appointments", "reminder_1h_sent_at", "reminder_1h_sent_at TIMESTAMP NULL")
# MySQL <8.0.13 rejects DEFAULT literals on TEXT/BLOB columns, so we add the
# column NULLABLE and let the backfill below normalise existing rows to '[]'.
_ensure_column("users", "permissions", "permissions TEXT NULL")


def _backfill_user_permissions():
    """Two jobs in one pass:

    1. Replace NULL/empty `permissions` values with the canonical "[]" so JSON
       parsers and the UI never see an empty string.
    2. For existing moderators (created before RBAC), grant the full section
       set so deploys don't silently lock them out — super admin can dial back
       per-user from the team UI.

    Both steps are idempotent and tolerant of a missing column (e.g. if the
    earlier _ensure_column call raised, we don't want a second exception
    here to abort startup)."""
    try:
        from sqlalchemy.orm import Session
        from database import SessionLocal
        from utils.permissions import ADMIN_SECTIONS, serialize_permissions

        db: Session = SessionLocal()
        try:
            # Step 1: normalise NULLs / empty strings to "[]"
            normalised = (
                db.query(models.User)
                .filter((models.User.permissions == None) | (models.User.permissions == ""))
                .update({models.User.permissions: "[]"}, synchronize_session=False)
            )

            # Step 2: backfill existing moderators with the full section set
            full = serialize_permissions(ADMIN_SECTIONS)
            promoted = (
                db.query(models.User)
                .filter(models.User.role == "moderator", models.User.permissions == "[]")
                .update({models.User.permissions: full}, synchronize_session=False)
            )

            if normalised or promoted:
                db.commit()
                logger.info(
                    "Permissions backfill: normalised=%d, granted-full=%d", normalised, promoted
                )
        finally:
            db.close()
    except Exception as e:
        logger.error("Permissions backfill failed: %s", e)


_backfill_user_permissions()


def _purge_expired_one_time_tokens():
    """Clean up expired reset:/verify: entries in site_settings on startup.
    These use the key-value table as a transient store; without cleanup the table grows
    forever and 6-digit OTP keyspace (1M) can eventually collide with a fresh OTP."""
    try:
        from datetime import datetime
        from sqlalchemy.orm import Session
        from database import SessionLocal
        db: Session = SessionLocal()
        try:
            rows = (
                db.query(models.SiteSetting)
                .filter(models.SiteSetting.key.like("reset:%") | models.SiteSetting.key.like("verify:%"))
                .all()
            )
            now = datetime.utcnow()
            removed = 0
            for row in rows:
                parts = (row.value or "").split(":", 1)
                if len(parts) != 2:
                    db.delete(row); removed += 1; continue
                try:
                    if datetime.fromisoformat(parts[1]) < now:
                        db.delete(row); removed += 1
                except ValueError:
                    db.delete(row); removed += 1
            if removed:
                db.commit()
                logger.info("Purged %d expired one-time tokens", removed)
        finally:
            db.close()
    except Exception as e:
        logger.error("Token purge failed: %s", e)


_purge_expired_one_time_tokens()


# ── Reminder scheduler ────────────────────────────────────────────────────────
# 5-minute background tick that emails + WhatsApps users 24h and 1h before
# their scheduled appointment. Idempotent — safe under --reload.
try:
    from utils.reminders import start_reminder_scheduler
    start_reminder_scheduler()
except Exception as _e:
    logger.error("Reminder scheduler init failed: %s", _e)

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="SPBSP, Ahilyanagar Consultation API",
    version="1.0.0",
    docs_url="/docs" if not settings.core.is_production else None,
    redoc_url=None,
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["200/minute"])
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


# ── Origin check middleware (CSRF defense for samesite=none cookies) ─────────
# In production the auth cookie is samesite="none" so it can flow on
# cross-site sub-resource requests (file proxy, frontend hosted on a different
# registrable domain). That re-opens CSRF for unpreflighted form/multipart
# POSTs from attacker-controlled pages. CORS only blocks XHR replays, not
# `<form action="…/admin/…">`. This middleware closes that gap by rejecting
# state-changing requests whose Origin/Referer doesn't match an allowed origin.

from urllib.parse import urlparse


class OriginCheckMiddleware(BaseHTTPMiddleware):
    STATE_CHANGING = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app, allowed_origins: set[str]):
        super().__init__(app)
        # Normalise once so we don't pay the .rstrip cost per request.
        self.allowed_origins = {o.rstrip("/") for o in allowed_origins if o}

    async def dispatch(self, request: Request, call_next):
        if request.method not in self.STATE_CHANGING:
            return await call_next(request)

        origin = (request.headers.get("origin") or "").rstrip("/")
        if origin:
            if origin not in self.allowed_origins:
                return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
        else:
            # No Origin header — most commonly server-to-server, healthchecks,
            # or non-browser clients. Fall back to Referer if present; otherwise
            # allow (Bearer-token clients, curl, internal callers don't ship
            # Origin and shouldn't be blocked here).
            referer = request.headers.get("referer") or ""
            if referer:
                try:
                    p = urlparse(referer)
                    referer_origin = f"{p.scheme}://{p.netloc}".rstrip("/")
                    if referer_origin not in self.allowed_origins:
                        return JSONResponse(status_code=403, content={"detail": "Referer not allowed"})
                except Exception:
                    pass
        return await call_next(request)


# ── Security headers middleware ───────────────────────────────────────────────
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        # Strict Transport Security in production — forces HTTPS for a year.
        if settings.core.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response


app.add_middleware(SecurityHeadersMiddleware)

# Origin check runs after CORS so that legitimate cross-origin fetches (which
# CORS already validates) reach this layer with their Origin header populated.
app.add_middleware(OriginCheckMiddleware, allowed_origins={settings.core.frontend_url})


# ── Admin rate limit middleware ──────────────────────────────────────────────
# Simple per-IP token bucket that throttles /admin/* to 120 requests / minute.
# Authenticated admins hitting multiple endpoints still get plenty of headroom,
# but a runaway script / compromised token can't hammer the server.
import time
from collections import defaultdict, deque

ADMIN_RATE_LIMIT = settings.core.admin_rate_limit_per_min
_admin_hits: dict[str, deque[float]] = defaultdict(deque)


class AdminRateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/admin/") or path.startswith("/admin"):
            ip = (request.client.host if request.client else "") or "unknown"
            now = time.monotonic()
            hits = _admin_hits[ip]
            window_start = now - 60.0
            while hits and hits[0] < window_start:
                hits.popleft()
            if len(hits) >= ADMIN_RATE_LIMIT:
                return JSONResponse(
                    status_code=429,
                    content={"detail": f"Rate limit exceeded ({ADMIN_RATE_LIMIT}/min on admin endpoints). Please slow down."},
                )
            hits.append(now)
        return await call_next(request)


app.add_middleware(AdminRateLimitMiddleware)


# ── CORS ──────────────────────────────────────────────────────────────────────
FRONTEND_URL = settings.core.frontend_url
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# ── Global exception handler ─────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled error: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={"detail": "An unexpected error occurred. Please try again."},
    )


# ── File serving ──────────────────────────────────────────────────────────────
# Replaced the prior public StaticFiles mount with an auth-aware proxy router
# (routers/files.py). Public CMS assets (uploads/pitham/*) still require no
# auth and get long-cache headers; personal files (documents, selfies, analysis,
# receipts, invoices) require authentication AND ownership.
os.makedirs("uploads", exist_ok=True)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(appointments.router)
app.include_router(users.router)
app.include_router(documents.router)
app.include_router(queries.router)
app.include_router(recordings.router)
app.include_router(user_lists.router)
app.include_router(payments.router)
app.include_router(settings_router.router)
app.include_router(analytics.router)
app.include_router(admin_tools.router)
app.include_router(events.router)
app.include_router(pitham.router)
app.include_router(broadcasts.router)
app.include_router(feedback.router)
app.include_router(files.router)


@app.get("/health")
def health():
    return {"status": "ok"}


logger.info("Pitham API started | CORS origin: %s", FRONTEND_URL)
logger.info("Integrations configured: %s", settings.summary())
