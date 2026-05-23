"""HMAC-signed URLs for /uploads/* file access.


Why this exists:


The /uploads/{path} route accepts auth via cookie or Bearer header. Both
break in common deployment shapes:


  - `<img src="https://api/.../selfie.jpg">` never sends the Authorization
    header because browsers don't fetch images with credentials by default.
  - On a cross-registrable-domain deployment (e.g. frontend on
    app.vercel.app, backend on api.onrender.com), the httpOnly session
    cookie won't flow on third-party-style requests — Safari ITP and
    Chrome's third-party cookie phaseout both block it.


The fix: a short-lived HMAC signature on the URL itself, e.g.
    /uploads/selfies/abc.jpg?exp=1716399100&sig=base64...


The signing endpoint (/files/sign) is auth-gated normally and verifies
the user actually owns each path before signing — so a signed URL is
guaranteed to point at a file the requester was authorised for at sign
time. Validity is capped at SIGNED_URL_TTL_SECONDS to keep the blast
radius of a leaked URL bounded.


Signature does NOT include the user identity — once minted, anyone with
the URL can fetch it for 60 seconds. That's an acceptable trade for
working <img> / <a> tags. For higher sensitivity (e.g. payment receipts
shared by accident in a screenshot), shorten the TTL or move that flow
to a fetch+blob download.
"""


from __future__ import annotations


import hashlib
import hmac
import time


from utils.auth import SECRET_KEY


# 60 seconds is enough to render a page and click a link; short enough
# that a URL pasted into a chat doesn't keep working tomorrow.
SIGNED_URL_TTL_SECONDS = 60




def _signing_key() -> bytes:
    # Reuse the JWT signing secret. If a future audit wants per-purpose
    # secrets, swap this for a namespace-derived key:
    # hashlib.sha256(b"file-signing:" + SECRET_KEY.encode()).digest()
    return SECRET_KEY.encode("utf-8")




def _payload(path: str, exp: int) -> bytes:
    # Sign the canonical path + expiry. Path is normalised to forward
    # slashes elsewhere; we don't lowercase it because case matters on
    # case-sensitive filesystems.
    return f"{path}|{exp}".encode("utf-8")




def make_signature(path: str, exp: int) -> str:
    digest = hmac.new(_signing_key(), _payload(path, exp), hashlib.sha256).digest()
    # urlsafe base64 without padding so the URL stays clean.
    return _b64_urlsafe(digest)




def sign_url(path: str, ttl_seconds: int = SIGNED_URL_TTL_SECONDS) -> tuple[int, str]:
    """Return (exp, sig) for `path`. Caller composes them as query params."""
    exp = int(time.time()) + ttl_seconds
    return exp, make_signature(path, exp)




def verify_signature(path: str, exp_str: str | None, sig: str | None) -> bool:
    """True if `sig` is a valid HMAC for `path` and the URL hasn't expired.


    Constant-time compare on the HMAC. Returns False on any malformed
    input rather than raising so callers can fall back to other auth.
    """
    if not exp_str or not sig:
        return False
    try:
        exp = int(exp_str)
    except (TypeError, ValueError):
        return False
    if exp < int(time.time()):
        return False
    expected = make_signature(path, exp)
    return hmac.compare_digest(expected, sig)




def _b64_urlsafe(data: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")
