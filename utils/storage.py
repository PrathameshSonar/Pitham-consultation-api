"""Storage abstraction — local filesystem today, swappable to S3/R2/GCS later
without touching call sites.

Routers should use:
    from utils.storage import storage
    path = await storage.save(category, filename, content)   # returns DB-storable path
    storage.delete(path)
    public_url = storage.public_url(path)                     # for frontend rendering

To switch backends, set STORAGE_DRIVER=s3 (or "local", default) and the matching
S3_* env vars. The local driver writes under backend/uploads/<category>/.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Optional

import aiofiles

logger = logging.getLogger("pitham.storage")


class StorageDriver(ABC):
    @abstractmethod
    async def save(self, category: str, filename: str, content: bytes) -> str: ...
    @abstractmethod
    def delete(self, path: str) -> None: ...
    @abstractmethod
    def public_url(self, path: str) -> str: ...


class LocalStorage(StorageDriver):
    """Saves files under backend/uploads/<category>/. Returns relative paths
    that the existing fileUrl() helper on the frontend already understands."""

    def __init__(self, base_dir: str = "uploads"):
        self.base_dir = base_dir
        os.makedirs(base_dir, exist_ok=True)

    async def save(self, category: str, filename: str, content: bytes) -> str:
        sub = os.path.join(self.base_dir, category)
        os.makedirs(sub, exist_ok=True)
        path = os.path.join(sub, filename)
        async with aiofiles.open(path, "wb") as f:
            await f.write(content)
        return path  # relative — frontend prefixes with API base via fileUrl()

    def delete(self, path: str) -> None:
        if not path or path.startswith(("http://", "https://")):
            return
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logger.warning("Failed to delete %s: %s", path, e)

    def public_url(self, path: str) -> str:
        # Files are served by FastAPI's StaticFiles mount at /uploads
        return f"/{path.replace(os.sep, '/')}" if path else ""


class S3Storage(StorageDriver):
    """Stub for future S3/R2/GCS migration. Wire up boto3 + AWS_* env vars."""

    def __init__(self):
        raise NotImplementedError(
            "S3 driver not implemented yet. Set STORAGE_DRIVER=local until S3 wiring is added."
        )

    async def save(self, category: str, filename: str, content: bytes) -> str:
        raise NotImplementedError

    def delete(self, path: str) -> None:
        raise NotImplementedError

    def public_url(self, path: str) -> str:
        raise NotImplementedError


def _build_driver() -> StorageDriver:
    from config import settings
    if settings.storage.driver == "s3":
        return S3Storage()
    return LocalStorage(base_dir=settings.storage.local_upload_dir)


# Singleton — import this everywhere
storage: StorageDriver = _build_driver()
