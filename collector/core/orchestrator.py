from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from pathlib import Path

from collector.core.coverage import CoverageTracker
from collector.core.dedupe import EntityResolver
from collector.core.enrichment import EnrichmentEngine
from collector.core.models import CityEntity, CrawlCandidate, CandidateKind
from collector.core.storage import JsonStore


@dataclass(order=True)
class QueueItem:
    sort_priority: float
    sequence: int
    candidate: CrawlCandidate = field(compare=False)


class SourceAdapter:
    name: str

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        raise NotImplementedError

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        raise NotImplementedError


class CrawlOrchestrator:
    def __init__(
        self,
        data_root: Path,
        adapters: list[SourceAdapter],
        max_depth: int = 3,
    ) -> None:
        self.store = JsonStore(data_root)
        self.adapters = adapters
        self.max_depth = max_depth
        self.resolver = EntityResolver()
        self.enrichment = EnrichmentEngine()
        self.coverage = CoverageTracker(self.store)
        self._sequence = 0

    def seed(self, candidates: list[CrawlCandidate]) -> None:
        frontier = self.store.load_frontier()
        frontier.extend(candidates)
        self.store.save_frontier(self._dedupe_candidates(frontier))

    def run(self, max_candidates: int = 100) -> None:
        frontier = self.store.load_frontier()
        queue = self._queue(frontier)
        processed = 0
        remaining: list[CrawlCandidate] = []
        seen_existing = list(self.store.iter_entities())

        while queue and processed < max_candidates:
            item = heapq.heappop(queue)
            candidate = item.candidate
            if candidate.depth > self.max_depth:
                continue
            adapter = self._adapter_for(candidate)
            if adapter is None:
                remaining.append(candidate)
                continue
            try:
                entities, new_candidates = adapter.crawl(candidate)
            except Exception as exc:
                candidate.metadata["last_error"] = str(exc)
                candidate.priority *= 0.5
                remaining.append(candidate)
                continue
            for entity in entities:
                matched = self.resolver.find_match(entity, seen_existing)
                if matched:
                    entity = self.resolver.merge(matched, entity)
                    seen_existing = [item for item in seen_existing if item.id != matched.id]
                entity.metadata = self.enrichment.enrich(entity)
                self.store.save_entity(entity)
                seen_existing.append(entity)
            for new_candidate in new_candidates:
                if new_candidate.depth <= self.max_depth:
                    heapq.heappush(queue, self._queue_item(new_candidate))
            processed += 1

        remaining.extend(item.candidate for item in queue)
        frontier = self._dedupe_candidates(remaining)
        self.store.save_frontier(frontier)
        self.coverage.snapshot(frontier)

    def bootstrap_hyderabad(self) -> None:
        seeds = [
            CrawlCandidate(
                kind=CandidateKind.QUERY,
                value=category,
                source="openstreetmap",
                priority=1.0,
                metadata={"city": "Hyderabad"},
            )
            for category in [
                "food",
                "nightlife",
                "sports",
                "entertainment",
                "culture",
                "religion",
                "education",
                "tourism",
                "shopping",
                "coworking",
                "startup",
                "ngo",
                "parks",
                "lakes",
                "museums",
                "theaters",
                "fitness",
            ]
        ]
        seeds.extend(
            [
                CrawlCandidate(kind=CandidateKind.QUERY, value="Hyderabad", source="wikipedia", priority=0.9),
                CrawlCandidate(kind=CandidateKind.QUERY, value="Hyderabad", source="wikidata", priority=0.9),
            ]
        )
        seeds.extend(
            CrawlCandidate(
                kind=CandidateKind.QUERY,
                value=query,
                source="google_search",
                priority=0.85,
                metadata={"city": "Hyderabad", "discovery_mode": "open_web"},
            )
            for query in [
                "Hyderabad events this week",
                "Hyderabad communities clubs volunteer groups",
                "Hyderabad hidden gems places to visit",
                "Hyderabad startup meetups coworking communities",
                "Hyderabad badminton football gaming activities",
                "Hyderabad temples museums parks lakes attractions",
                "Hyderabad food nightlife cafes restaurants",
            ]
        )
        self.seed(seeds)

    def _adapter_for(self, candidate: CrawlCandidate) -> SourceAdapter | None:
        for adapter in self.adapters:
            if adapter.can_handle(candidate):
                return adapter
        return None

    def _queue(self, candidates: list[CrawlCandidate]) -> list[QueueItem]:
        queue = [self._queue_item(candidate) for candidate in self._dedupe_candidates(candidates)]
        heapq.heapify(queue)
        return queue

    def _queue_item(self, candidate: CrawlCandidate) -> QueueItem:
        self._sequence += 1
        return QueueItem(-candidate.priority, self._sequence, candidate)

    def _dedupe_candidates(self, candidates: list[CrawlCandidate]) -> list[CrawlCandidate]:
        seen: set[tuple[str, str, str]] = set()
        out: list[CrawlCandidate] = []
        for candidate in sorted(candidates, key=lambda item: item.priority, reverse=True):
            key = (candidate.kind.value, candidate.source, candidate.value.lower())
            if key not in seen:
                out.append(candidate)
                seen.add(key)
        return out
