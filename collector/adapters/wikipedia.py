from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore
from collector.core.structured_extraction import StructuredExtractor


logger = logging.getLogger(__name__)


class WikipediaAdapter(SourceAdapter):
    name = "wikipedia"

    def __init__(self, http: PoliteHttpClient, store: JsonStore, limit: int = 40) -> None:
        self.http = http
        self.store = store
        self.limit = limit
        self.extractor = StructuredExtractor()

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        logger.info("searching wikipedia query=%s depth=%s", candidate.value, candidate.depth)
        search = self.http.get_json(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": f"{candidate.value} Hyderabad",
                "format": "json",
                "srlimit": min(self.limit, 20),
            },
        )
        raw_path = self.store.save_raw(self.name, f"search-{candidate.value}-{candidate.depth}", search)
        results = ((search or {}).get("query") or {}).get("search", [])  # type: ignore[union-attr]
        entities: list[CityEntity] = []
        new_candidates: list[CrawlCandidate] = []
        for result in results:
            title = result.get("title")
            if not title:
                continue
            snippet = BeautifulSoup(result.get("snippet", ""), "html.parser").get_text(" ")
            page_id = str(result.get("pageid"))
            page_url = f"https://en.wikipedia.org/?curid={page_id}"
            entities.append(
                CityEntity(
                    id=entity_id(title, "Hyderabad"),
                    name=title,
                    category="knowledge_article",
                    description=snippet,
                    locality="Hyderabad",
                    website=page_url,
                    sources=[
                        SourceRecord(
                            source=self.name,
                            url=page_url,
                            source_id=page_id,
                            license="CC BY-SA",
                            raw_path=raw_path,
                            source_type="public_api",
                            source_name="Wikipedia",
                            canonical_url=page_url,
                            search_query=candidate.value,
                            crawl_status="success",
                            extraction_confidence=0.55,
                        )
                    ],
                )
            )
            new_candidates.append(
                CrawlCandidate(
                    kind=CandidateKind.QUERY,
                    value=title,
                    source="wikidata",
                    priority=0.4,
                    depth=candidate.depth + 1,
                )
            )
        enriched = []
        for entity in entities:
            entity, reviews, relationships = self.extractor.enrich_from_document(
                entity,
                text=" ".join([entity.name, entity.description or "", entity.locality or ""]),
                url=str(entity.website or ""),
            )
            if reviews:
                self.store.append_reviews(entity.id, reviews)
            if relationships:
                self.store.append_relationships(entity.id, relationships)
            enriched.append(entity)
        logger.info("wikipedia extracted query=%s entities=%s new_candidates=%s", candidate.value, len(enriched), len(new_candidates))
        return enriched, new_candidates
