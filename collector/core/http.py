from __future__ import annotations

import time
import logging
import random
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from collector.core.config import settings


logger = logging.getLogger(__name__)


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    content_type: str | None


class PoliteHttpClient:
    def __init__(
        self,
        user_agent: str,
        timeout_seconds: float = 30.0,
        min_delay_seconds: float = 1.0,
        respect_robots: bool = True,
    ) -> None:
        self.user_agent = user_agent
        self.min_delay_seconds = min_delay_seconds
        self.respect_robots = respect_robots
        self._last_fetch: dict[str, float] = {}
        self._rate_limited_until: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}
        self._lock = threading.RLock()
        self._client = httpx.Client(
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        )

    def get_json(self, url: str, params: dict[str, object] | None = None) -> object:
        response = self._request("GET", url, params=params)
        return response.json()

    def post_json(self, url: str, payload: dict[str, object], headers: dict[str, str] | None = None) -> object:
        request_headers = {"Content-Type": "application/json"}
        if headers:
            request_headers.update(headers)
        response = self._request("POST", url, json=payload, headers=request_headers)
        return response.json()

    def get_text(self, url: str, params: dict[str, object] | None = None) -> FetchResult:
        response = self._request("GET", url, params=params)
        return FetchResult(
            url=str(response.url),
            status_code=response.status_code,
            text=response.text,
            content_type=response.headers.get("content-type"),
        )

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        if self.respect_robots and not self._allowed_by_robots(url):
            logger.warning("robots blocked method=%s url=%s", method, url)
            raise PermissionError(f"Blocked by robots.txt: {url}")
        self._wait_for_domain(url)
        for attempt in range(4):
            try:
                logger.info("http request method=%s url=%s attempt=%s", method, url, attempt + 1)
                response = self._client.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    if response.status_code == 429:
                        self._mark_rate_limited(url, response)
                    raise httpx.HTTPStatusError(
                        f"retryable status {response.status_code} for {response.url}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                logger.info("http response status=%s url=%s", response.status_code, response.url)
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
                if attempt == 3:
                    logger.error("http failed method=%s url=%s error=%s", method, url, exc)
                    raise
                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "http retry method=%s url=%s attempt=%s delay=%.2fs error=%s",
                    method,
                    url,
                    attempt + 1,
                    delay,
                    exc,
                )
                time.sleep(delay)
        raise RuntimeError("unreachable")

    def _wait_for_domain(self, url: str) -> None:
        domain = urlparse(url).netloc
        while True:
            with self._lock:
                now = time.monotonic()
                rate_wait = max(0.0, self._rate_limited_until.get(domain, 0.0) - now)
                elapsed = now - self._last_fetch.get(domain, 0.0)
                polite_wait = max(0.0, self.min_delay_seconds - elapsed)
                wait = max(rate_wait, polite_wait)
                if wait <= 0:
                    self._last_fetch[domain] = time.monotonic()
                    return
            logger.debug("domain wait domain=%s seconds=%.2f", domain, wait)
            time.sleep(wait)

    def _mark_rate_limited(self, url: str, response: httpx.Response) -> None:
        domain = urlparse(url).netloc
        retry_after = self._retry_after_seconds(response)
        with self._lock:
            until = time.monotonic() + retry_after
            self._rate_limited_until[domain] = max(self._rate_limited_until.get(domain, 0.0), until)
        logger.warning("http 429 rate limited domain=%s retry_after=%.2fs", domain, retry_after)

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            if exc.response.status_code == 429:
                return self._retry_after_seconds(exc.response)
        return min(float(settings.max_retry_after_seconds), (2**attempt) + random.uniform(0.25, 1.25))

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

    def _allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return True
        root = f"{parsed.scheme}://{parsed.netloc}"
        parser = self._robots.get(root)
        if parser is None:
            with self._lock:
                parser = self._robots.get(root)
                if parser is None:
                    parser = RobotFileParser()
                    parser.set_url(f"{root}/robots.txt")
                    try:
                        parser.read()
                    except Exception:
                        return True
                    self._robots[root] = parser
        return parser.can_fetch(self.user_agent, url)
