from __future__ import annotations

from collections import Counter

from collector.core.models import CoverageSnapshot, CrawlCandidate
from collector.core.storage import JsonStore


class CoverageTracker:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    def snapshot(self, frontier: list[CrawlCandidate]) -> CoverageSnapshot:
        entities = list(self.store.iter_entities())
        by_category = Counter(entity.category or "unknown" for entity in entities)
        by_locality = Counter(entity.locality or "unknown" for entity in entities)
        by_source: Counter[str] = Counter()
        for entity in entities:
            by_source.update(record.source for record in entity.sources)
        snapshot = CoverageSnapshot(
            entities_total=len(entities),
            by_category=dict(sorted(by_category.items())),
            by_locality=dict(sorted(by_locality.items())),
            by_source=dict(sorted(by_source.items())),
            crawl_frontier_size=len(frontier),
            plateau_signal=self._plateau_signal(frontier, len(entities)),
        )
        self.store.write_index("coverage.json", snapshot.model_dump(mode="json"))
        return snapshot

    def _plateau_signal(self, frontier: list[CrawlCandidate], entity_count: int) -> float:
        if entity_count == 0:
            return 0.0
        return round(max(0.0, 1.0 - min(len(frontier), entity_count) / entity_count), 3)
