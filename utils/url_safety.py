"""Validate user-/admin-supplied URLs before persisting them.


Admins paste URLs into many form fields (event image_url, location_map_url,
zoom_link, social links). Without validation, an admin compromise (or a
moderator with mischief) could store:


  - javascript: URLs       → XSS when the value is rendered as href
  - http://169.254.169.254 → SSRF reach to AWS metadata if any server-side
                             process ever fetches the URL
  - http://10.0.0.x        → SSRF reach to internal services on RFC1918 nets
  - file:// / data:        → file disclosure / XSS


This module rejects all of the above. It does NOT verify that the URL
actually resolves to something useful — that's a separate concern.
"""


from __future__ import annotations


import ipaddress
import re
from urllib.parse import urlparse


# Anything that's clearly not an external https/http URL. javascript: and
# vbscript: are explicit XSS vectors; data: and file: are file-disclosure /
# content-spoofing vectors; intent: / about: / ftp: have no business here.
_ALLOWED_SCHEMES = {"http", "https"}


# Hostnames we never want stored — even an admin shouldn't be able to point
# event image_url at a private network. The block also catches the common
# cloud-metadata service IPs.
_BLOCKED_HOSTNAMES = {
    "localhost",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
}


# A relaxed hostname regex — RFC says A-Z 0-9 - . The point isn't perfect
# validation, just rejecting obviously malformed input early.
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,253}$")




def _hostname_is_private(host: str) -> bool:
    """Return True if `host` resolves to a non-routable address class.


    Doesn't do DNS lookups — only catches literal IP strings. A determined
    attacker who registers a public hostname pointing at 127.0.0.1 will
    bypass this; the proper fix is to also resolve + check at fetch time,
    which is the responsibility of any server-side fetcher built later.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host.lower() in _BLOCKED_HOSTNAMES
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )




class UnsafeUrl(ValueError):
    """Raised when the URL fails one of the safety checks. Callers should
    surface the message to the admin so they understand why the form was
    rejected."""




def validate_external_url(
    url: str | None,
    *,
    allow_empty: bool = True,
    require_https: bool = True,
) -> str:
    """Return a cleaned URL string after rejecting unsafe values.


    `allow_empty=True` (default) returns "" for None/empty input — useful
    when the URL is optional. Set False to require a non-empty value.


    `require_https=True` (default) rejects plain-text http:// because every
    URL we accept here is rendered to end-users; downgrading them is bad.
    Local dev sometimes needs http (e.g. testing maps with local-tile-server)
    so the override is available.
    """
    if not url or not url.strip():
        if allow_empty:
            return ""
        raise UnsafeUrl("URL is required")
    raw = url.strip()
    # Forbid embedded whitespace / control chars — common in copy-paste
    # attacks that try to hide a javascript: payload after a newline.
    if any(c in raw for c in "\r\n\t") or " " in raw:
        raise UnsafeUrl("URL must not contain whitespace")


    try:
        parsed = urlparse(raw)
    except ValueError as e:
        raise UnsafeUrl(f"URL is malformed: {e}") from e


    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UnsafeUrl(f"URL scheme must be http or https (got {scheme!r})")
    if require_https and scheme != "https":
        raise UnsafeUrl("URL must use https://")


    host = (parsed.hostname or "").strip()
    if not host:
        raise UnsafeUrl("URL must include a hostname")
    if not _HOSTNAME_RE.match(host):
        raise UnsafeUrl("URL hostname contains invalid characters")
    if _hostname_is_private(host):
        raise UnsafeUrl(
            "URL points at a private/internal network — rejected for safety"
        )


    return raw
