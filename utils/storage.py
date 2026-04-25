"""Storage abstraction — local filesystem OR S3 (any S3-compatible: AWS, Cloudflare R2,
DigitalOcean Spaces, Backblaze B2, MinIO).

Switch backends with a single env var:
    STORAGE_DRIVER=local   (default — backend/uploads/)
    STORAGE_DRIVER=s3      (production)

Routers use:
    from utils.storage import storage
    path = await storage.save("category", "filename.jpg", content_bytes)
    storage.delete(path)
    public_url = storage.public_url(path)
"""

from __future__ import annotations

import logging
import mimetypes
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
        return path

    def delete(self, path: str) -> None:
        if not path or path.startswith(("http://", "https://")):
            return
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError as e:
                logger.warning("Failed to delete %s: %s", path, e)

    def public_url(self, path: str) -> str:
        return f"/{path.replace(os.sep, '/')}" if path else ""


class S3Storage(StorageDriver):
    """S3 / S3-compatible. Stores bucket-relative keys; public_url joins with the
    public base URL (CDN, R2 public dev URL, or s3.amazonaws.com path).

    For Cloudflare R2 (recommended — zero egress fees):
      S3_ENDPOINT_URL=https://<account-id>.r2.cloudflarestorage.com
      S3_REGION=auto
      S3_PUBLIC_BASE_URL=https://<bucket>.<account-id>.r2.dev
    """

    def __init__(self):
        from config import settings
        cfg = settings.storage
        if not (cfg.s3_bucket and cfg.s3_access_key and cfg.s3_secret_key):
            raise RuntimeError(
                "S3 driver requires S3_BUCKET, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY in .env"
            )
        try:
            import boto3
            from botocore.config import Config as BotoConfig
        except ImportError as e:
            raise RuntimeError(
                "boto3 is required for STORAGE_DRIVER=s3. Add it to requirements.txt."
            ) from e

        self._cfg = cfg
        self._client = boto3.client(
            "s3",
            region_name=cfg.s3_region or "auto",
            endpoint_url=cfg.s3_endpoint_url or None,
            aws_access_key_id=cfg.s3_access_key,
            aws_secret_access_key=cfg.s3_secret_key,
            config=BotoConfig(signature_version="s3v4"),
        )
        logger.info("S3 storage active: bucket=%s endpoint=%s",
                    cfg.s3_bucket, cfg.s3_endpoint_url or "aws-default")

    async def save(self, category: str, filename: str, content: bytes) -> str:
        key = f"{category}/{filename}"
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        # boto3 is sync — but uploads are small enough that wrapping in a thread
        # is overkill; prod traffic goes via Sentry to monitor latency
        self._client.put_object(
            Bucket=self._cfg.s3_bucket,
            Key=key,
            Body=content,
            ContentType=ctype,
            CacheControl="public, max-age=31536000, immutable",  # 1-year cache; filenames are unique
        )
        return key

    def delete(self, path: str) -> None:
        if not path or path.startswith(("http://", "https://")):
            return
        try:
            self._client.delete_object(Bucket=self._cfg.s3_bucket, Key=path)
        except Exception as e:
            logger.warning("Failed to S3-delete %s: %s", path, e)

    def public_url(self, path: str) -> str:
        if not path:
            return ""
        if path.startswith(("http://", "https://")):
            return path
        base = self._cfg.s3_public_base_url
        if base:
            return f"{base.rstrip('/')}/{path}"
        # AWS path-style fallback
        return f"https://{self._cfg.s3_bucket}.s3.amazonaws.com/{path}"


def _build_driver() -> StorageDriver:
    from config import settings
    if settings.storage.driver == "s3":
        return S3Storage()
    return LocalStorage(base_dir=settings.storage.local_upload_dir)


# Singleton
storage: StorageDriver = _build_driver()
