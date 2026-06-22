from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx


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
        self._robots: dict[str, RobotFileParser] = {}
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
            raise PermissionError(f"Blocked by robots.txt: {url}")
        self._wait_for_domain(url)
        for attempt in range(4):
            try:
                response = self._client.request(method, url, **kwargs)
                if response.status_code in {429, 500, 502, 503, 504}:
                    raise httpx.HTTPStatusError("retryable status", request=response.request, response=response)
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError):
                if attempt == 3:
                    raise
                time.sleep(2**attempt)
        raise RuntimeError("unreachable")

    def _wait_for_domain(self, url: str) -> None:
        domain = urlparse(url).netloc
        elapsed = time.monotonic() - self._last_fetch.get(domain, 0.0)
        if elapsed < self.min_delay_seconds:
            time.sleep(self.min_delay_seconds - elapsed)
        self._last_fetch[domain] = time.monotonic()

    def _allowed_by_robots(self, url: str) -> bool:
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return True
        root = f"{parsed.scheme}://{parsed.netloc}"
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
