from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone

import httpx

from collector.core.config import settings


logger = logging.getLogger(__name__)


@dataclass
class DownloadedImage:
    url: str
    data: bytes
    mime_type: str
    content_hash: str

    @property
    def byte_size(self) -> int:
        return len(self.data)


class ImageBlobDownloader:
    def __init__(self) -> None:
        self._client = httpx.Client(
            timeout=settings.image_download_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )

    def download(self, url: str) -> DownloadedImage | None:
        if not url.startswith(("http://", "https://")):
            return None
        for attempt in range(3):
            try:
                logger.info("image download url=%s attempt=%s", url, attempt + 1)
                with self._client.stream("GET", url) as response:
                    if response.status_code == 429 and attempt < 2:
                        delay = self._retry_after_seconds(response)
                        logger.warning("image rate limited url=%s retry_after=%.2fs", url, delay)
                        time.sleep(delay)
                        continue
                    response.raise_for_status()
                    mime_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not mime_type.startswith("image/"):
                        logger.warning("image skipped non-image content_type=%s url=%s", mime_type, url)
                        return None
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > settings.image_download_max_bytes:
                            logger.warning("image skipped too_large bytes=%s url=%s", size, url)
                            return None
                        chunks.append(chunk)
                data = b"".join(chunks)
                if not data:
                    return None
                content_hash = hashlib.sha256(data).hexdigest()
                return DownloadedImage(url=url, data=data, mime_type=mime_type, content_hash=content_hash)
            except Exception as exc:
                if attempt == 2:
                    logger.warning("image download failed url=%s error=%s", url, exc)
                    return None
                delay = min(float(settings.max_retry_after_seconds), 2.0 * (attempt + 1))
                logger.warning("image retry url=%s delay=%.2fs error=%s", url, delay, exc)
                time.sleep(delay)
        return None

    def _retry_after_seconds(self, response: httpx.Response) -> float:
        value = response.headers.get("retry-after")
        if not value:
            return min(float(settings.max_retry_after_seconds), 30.0)
        try:
            return min(float(settings.max_retry_after_seconds), max(1.0, float(value)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                seconds = (parsed - datetime.now(timezone.utc)).total_seconds()
                return min(float(settings.max_retry_after_seconds), max(1.0, seconds))
            except Exception:
                return min(float(settings.max_retry_after_seconds), 30.0)
