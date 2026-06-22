from __future__ import annotations

from collector.core.config import settings
from collector.core.http import PoliteHttpClient
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore


class GoogleSearchAdapter(SourceAdapter):
    name = "google_search"

    def __init__(self, http: PoliteHttpClient, store: JsonStore) -> None:
        self.http = http
        self.store = store

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        if not settings.google_custom_search_api_key or not settings.google_custom_search_engine_id:
            raise RuntimeError(
                "Google search is not configured. Set GOOGLE_CUSTOM_SEARCH_API_KEY and "
                "GOOGLE_CUSTOM_SEARCH_ENGINE_ID in .env to use the official Custom Search JSON API."
            )
        query = self._query(candidate.value)
        payload = self.http.get_json(
            "https://www.googleapis.com/customsearch/v1",
            params={
                "key": settings.google_custom_search_api_key,
                "cx": settings.google_custom_search_engine_id,
                "q": query,
                "num": min(max(settings.web_search_results_per_query, 1), 10),
            },
        )
        self.store.save_raw(self.name, f"{candidate.value}-{candidate.depth}", payload)
        items = (payload or {}).get("items", [])  # type: ignore[union-attr]
        new_candidates: list[CrawlCandidate] = []
        for rank, item in enumerate(items):
            url = item.get("link")
            if not url:
                continue
            new_candidates.append(
                CrawlCandidate(
                    kind=CandidateKind.SOURCE_URL,
                    source="web_page",
                    value=url,
                    priority=max(0.1, candidate.priority - rank * 0.03),
                    depth=candidate.depth + 1,
                    metadata={
                        "search_query": query,
                        "title": item.get("title"),
                        "snippet": item.get("snippet"),
                        "source_search": self.name,
                    },
                )
            )
        return [], new_candidates

    def _query(self, value: str) -> str:
        lower = value.lower()
        if "hyderabad" in lower:
            return value
        return f"{value} Hyderabad"
