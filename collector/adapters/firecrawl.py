from __future__ import annotations

import re
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from collector.adapters.web_page import CATEGORY_HINTS, HYDERABAD_TERMS
from collector.core.config import settings
from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore


class FirecrawlSearchAdapter(SourceAdapter):
    name = "firecrawl_search"

    def __init__(self, http: PoliteHttpClient, store: JsonStore) -> None:
        self.http = http
        self.store = store

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        query = self._query(candidate.value)
        payload = {
            "query": query,
            "limit": min(max(settings.firecrawl_search_limit, 1), 20),
        }
        result = self._post("/v2/search", payload)
        raw_path = self.store.save_raw(self.name, f"{candidate.value}-{candidate.depth}", result)
        items = self._items(result)
        entities: list[CityEntity] = []
        new_candidates: list[CrawlCandidate] = []
        for rank, item in enumerate(items):
            url = self._get(item, "url") or self._get(item, "link")
            title = self._get(item, "title") or url
            markdown = self._get(item, "markdown") or self._get(item, "description") or self._get(item, "snippet")
            if url:
                new_candidates.append(
                    CrawlCandidate(
                        kind=CandidateKind.SOURCE_URL,
                        source="firecrawl_page",
                        value=url,
                        priority=max(0.1, candidate.priority - rank * 0.03),
                        depth=candidate.depth + 1,
                        metadata={
                            "search_query": query,
                            "title": title,
                            "snippet": self._get(item, "description") or self._get(item, "snippet"),
                            "source_search": self.name,
                        },
                    )
                )
            if title and markdown and self._is_hyderabad_relevant(f"{title} {markdown}", url or ""):
                entities.append(
                    CityEntity(
                        id=entity_id(title, self._locality(markdown)),
                        name=title,
                        category=self._category(title, markdown),
                        description=self._summary(markdown),
                        locality=self._locality(markdown),
                        website=url,
                        sources=[
                            SourceRecord(
                                source=self.name,
                                url=url,
                                raw_path=raw_path,
                                metadata={"search_query": query, "rank": rank, "result": item},
                            )
                        ],
                    )
                )
        return entities, new_candidates

    def _post(self, path: str, payload: dict[str, object]) -> object:
        return self.http.post_json(self._url(path), payload, headers=self._headers())

    def _url(self, path: str) -> str:
        return f"{settings.firecrawl_base_url.rstrip('/')}{path}"

    def _headers(self) -> dict[str, str]:
        if settings.firecrawl_api_key:
            return {"Authorization": f"Bearer {settings.firecrawl_api_key}"}
        return {}

    def _query(self, value: str) -> str:
        if "hyderabad" in value.lower():
            return value
        return f"{value} Hyderabad"

    def _items(self, result: object) -> list[dict[str, object]]:
        if isinstance(result, list):
            return [item for item in result if isinstance(item, dict)]
        if not isinstance(result, dict):
            return []
        for key in ["data", "results", "items"]:
            value = result.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                items: list[dict[str, object]] = []
                for nested_key in ["web", "images", "news"]:
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        items.extend(item for item in nested if isinstance(item, dict))
                if items:
                    return items
        return []

    def _get(self, item: dict[str, object], key: str) -> str | None:
        value = item.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else None

    def _summary(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()[:600]

    def _category(self, title: str, text: str) -> str:
        haystack = f"{title} {text[:2000]}".lower()
        for category, hints in CATEGORY_HINTS.items():
            if any(re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", haystack) for hint in hints):
                return category
        return "web_entity"

    def _locality(self, text: str) -> str | None:
        lower = text.lower()
        for term in HYDERABAD_TERMS:
            if term in lower and term != "hyderabad":
                return term.title()
        return "Hyderabad" if "hyderabad" in lower else None

    def _is_hyderabad_relevant(self, text: str, url: str) -> bool:
        lower = f"{text} {url}".lower()
        return any(term in lower for term in HYDERABAD_TERMS)


class FirecrawlPageAdapter(FirecrawlSearchAdapter):
    name = "firecrawl_page"

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.SOURCE_URL

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        payload = {
            "url": candidate.value,
            "formats": settings.firecrawl_scrape_formats,
            "onlyMainContent": True,
        }
        result = self._post("/v2/scrape", payload)
        raw_path = self.store.save_raw(self.name, self._raw_key(candidate.value), result)
        data = self._data(result)
        markdown = self._get(data, "markdown") or ""
        html = self._get(data, "html") or ""
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        title = self._get(metadata, "title") or self._title_from_html(html) or candidate.metadata.get("title")
        description = (
            self._get(metadata, "description")
            or candidate.metadata.get("snippet")
            or self._summary(markdown or self._text_from_html(html))
        )
        text = f"{title or ''} {description or ''} {markdown or self._text_from_html(html)}"
        if not title or not self._is_hyderabad_relevant(text, candidate.value):
            return [], []
        entity = CityEntity(
            id=entity_id(str(title), self._locality(text)),
            name=str(title),
            category=self._category(str(title), text),
            description=str(description) if description else None,
            locality=self._locality(text),
            website=candidate.value,
            sources=[
                SourceRecord(
                    source=self.name,
                    url=candidate.value,
                    raw_path=raw_path,
                    metadata={
                        "firecrawl_metadata": metadata,
                        "parent": candidate.metadata,
                        "markdown_preview": markdown[:1200],
                    },
                )
            ],
        )
        new_candidates = self._link_candidates(data, candidate)
        return [entity], new_candidates

    def _data(self, result: object) -> dict[str, object]:
        if isinstance(result, dict):
            data = result.get("data")
            if isinstance(data, dict):
                return data
            return result
        return {}

    def _link_candidates(self, data: dict[str, object], candidate: CrawlCandidate) -> list[CrawlCandidate]:
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        links = data.get("links") or metadata.get("links") if isinstance(metadata, dict) else []
        if not isinstance(links, list):
            return []
        out: list[CrawlCandidate] = []
        parent_domain = urlparse(candidate.value).netloc.lower()
        for link in links[: settings.web_page_max_links]:
            url = link.get("url") if isinstance(link, dict) else str(link)
            if not url or not url.startswith(("http://", "https://")):
                continue
            domain = urlparse(url).netloc.lower()
            if domain != parent_domain and not self._is_hyderabad_relevant("", url):
                continue
            out.append(
                CrawlCandidate(
                    kind=CandidateKind.SOURCE_URL,
                    source=self.name,
                    value=url,
                    priority=max(0.05, candidate.priority - 0.15),
                    depth=candidate.depth + 1,
                    metadata={"parent_url": candidate.value, "source": "firecrawl_links"},
                )
            )
        return out

    def _title_from_html(self, html: str) -> str | None:
        if not html:
            return None
        soup = BeautifulSoup(html, "html.parser")
        if soup.title and soup.title.string:
            return re.sub(r"\s+", " ", soup.title.string).strip()
        h1 = soup.find("h1")
        return re.sub(r"\s+", " ", h1.get_text(" ")).strip() if h1 else None

    def _text_from_html(self, html: str) -> str:
        if not html:
            return ""
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return re.sub(r"\s+", " ", soup.get_text(" ")).strip()

    def _raw_key(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.netloc}-{parsed.path}".strip("-/") or parsed.netloc
