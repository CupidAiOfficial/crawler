from __future__ import annotations

from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore


class WikidataAdapter(SourceAdapter):
    name = "wikidata"

    def __init__(self, http: PoliteHttpClient, store: JsonStore, limit: int = 50) -> None:
        self.http = http
        self.store = store
        self.limit = limit

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        payload = self.http.get_json(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbsearchentities",
                "search": f"{candidate.value} Hyderabad",
                "language": "en",
                "format": "json",
                "limit": min(self.limit, 50),
            },
        )
        raw_path = self.store.save_raw(self.name, f"search-{candidate.value}-{candidate.depth}", payload)
        rows = (payload or {}).get("search", [])  # type: ignore[union-attr]
        entities: list[CityEntity] = []
        for row in rows:
            label = row.get("label")
            if not label:
                continue
            qid = row.get("id")
            description = row.get("description")
            entities.append(
                CityEntity(
                    id=entity_id(label, "Hyderabad"),
                    name=label,
                    aliases=row.get("aliases", []),
                    category="wikidata_entity",
                    description=description,
                    locality="Hyderabad",
                    website=row.get("concepturi"),
                    sources=[
                        SourceRecord(
                            source=self.name,
                            url=row.get("concepturi"),
                            source_id=qid,
                            license="CC0",
                            raw_path=raw_path,
                            metadata=row,
                        )
                    ],
                )
            )
        return entities, []
