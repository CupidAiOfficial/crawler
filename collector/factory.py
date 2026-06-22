from __future__ import annotations

from pathlib import Path

from collector.adapters import (
    FirecrawlPageAdapter,
    FirecrawlSearchAdapter,
    GoogleSearchAdapter,
    OpenStreetMapAdapter,
    WebPageAdapter,
    WikidataAdapter,
    WikipediaAdapter,
)
from collector.core.config import settings
from collector.core.http import PoliteHttpClient
from collector.core.orchestrator import CrawlOrchestrator
from collector.core.storage import JsonStore


def build_orchestrator(data_root: Path | None = None) -> CrawlOrchestrator:
    root = data_root or settings.data_root
    store = JsonStore(root)
    http = PoliteHttpClient(
        user_agent=settings.user_agent,
        min_delay_seconds=settings.request_delay_seconds,
        respect_robots=settings.respect_robots,
    )
    adapters = [
        FirecrawlSearchAdapter(http=http, store=store),
        FirecrawlPageAdapter(http=http, store=store),
        GoogleSearchAdapter(http=http, store=store),
        OpenStreetMapAdapter(http=http, store=store),
        WebPageAdapter(http=http, store=store),
        WikipediaAdapter(http=http, store=store),
        WikidataAdapter(http=http, store=store),
    ]
    return CrawlOrchestrator(data_root=root, adapters=adapters, max_depth=settings.max_depth)
