"""Shared helpers for validating and saving user-uploaded files."""

import os
import re
from typing import Iterable, Optional

from fastapi import HTTPException, UploadFile

# MIME whitelists — keep narrow. Document uploads are intentionally stricter
# than "any file" to prevent users/admins from uploading executables.
IMAGE_MIMES = {
    "image/jpeg", "image/png", "image/webp", "image/gif",
}
DOCUMENT_MIMES = {
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain",
    # Images are also ok as sadhna material
    *IMAGE_MIMES,
}

# Files with these extensions are always rejected regardless of declared MIME.
DANGEROUS_EXTS = {
    ".exe", ".bat", ".cmd", ".com", ".scr", ".ps1", ".sh",
    ".js", ".html", ".htm", ".svg", ".phtml", ".php", ".jsp",
    ".py", ".pl", ".rb",
}


def _ext(name: str) -> str:
    return os.path.splitext(name or "")[1].lower()


def safe_filename(name: str) -> str:
    """Sanitize — strip path separators and keep only safe chars."""
    return re.sub(r"[^\w.\-]", "_", os.path.basename(name or "file"))


def validate_upload(
    file: UploadFile,
    allowed_mimes: Iterable[str],
    max_bytes: int,
    label: str = "File",
) -> None:
    """Raise 400 if the upload isn't allowed. Does NOT read the body.
    Call after you've read content to also check size, or pre-check the declared MIME."""
    if not file or not file.filename:
        raise HTTPException(status_code=400, detail=f"{label} is required")

    ext = _ext(file.filename)
    if ext in DANGEROUS_EXTS:
        raise HTTPException(status_code=400, detail=f"{label}: file type not allowed")

    declared = (file.content_type or "").lower()
    allowed = {m.lower() for m in allowed_mimes}
    # application/octet-stream is sometimes sent when browser can't determine MIME;
    # only allow if extension also checks out
    if declared and declared != "application/octet-stream" and declared not in allowed:
        raise HTTPException(status_code=400, detail=f"{label}: unsupported type ({declared})")


def check_size(content: bytes, max_bytes: int, label: str = "File") -> None:
    if len(content) > max_bytes:
        raise HTTPException(
            status_code=400,
            detail=f"{label} must be under {max_bytes // (1024*1024)}MB.",
        )
