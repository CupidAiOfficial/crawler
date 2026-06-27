from __future__ import annotations

import heapq
import logging
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from collector.core.coverage import CoverageTracker
from collector.core.dedupe import EntityResolver
from collector.core.enrichment import EnrichmentEngine
from collector.core.models import CityEntity, CrawlCandidate, CandidateKind
from collector.core.quality import ProductionReadinessValidator
from collector.core.storage import JsonStore


logger = logging.getLogger(__name__)


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
        store: JsonStore | None = None,
    ) -> None:
        self.store = store or JsonStore(data_root)
        self.adapters = adapters
        self.max_depth = max_depth
        self.resolver = EntityResolver()
        self.enrichment = EnrichmentEngine()
        self.validator = ProductionReadinessValidator()
        self.coverage = CoverageTracker(self.store)
        self._sequence = 0

    def seed(self, candidates: list[CrawlCandidate]) -> None:
        logger.info("seeding candidates count=%s", len(candidates))
        frontier = self.store.load_frontier()
        frontier.extend(candidates)
        self.store.save_frontier(self._dedupe_candidates(frontier))

    def run(self, max_candidates: int = 100, workers: int = 1) -> None:
        if workers > 1:
            self._run_parallel(max_candidates=max_candidates, workers=workers)
            return
        frontier = self.store.load_frontier()
        logger.info("crawl batch start frontier=%s max_candidates=%s", len(frontier), max_candidates)
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
                logger.warning("no adapter candidate source=%s kind=%s value=%s", candidate.source, candidate.kind, candidate.value)
                remaining.append(candidate)
                continue
            try:
                logger.info(
                    "processing candidate source=%s kind=%s depth=%s priority=%.3f value=%s",
                    candidate.source,
                    candidate.kind,
                    candidate.depth,
                    candidate.priority,
                    candidate.value,
                )
                entities, new_candidates = adapter.crawl(candidate)
            except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError, PermissionError) as exc:
                candidate.metadata["last_error"] = str(exc)
                retry_count = int(candidate.metadata.get("retry_count", 0)) + 1
                candidate.metadata["retry_count"] = retry_count
                if self._should_retry_candidate(candidate, retry_count):
                    candidate.priority *= 0.5
                    remaining.append(candidate)
                    logger.warning(
                        "candidate deferred source=%s kind=%s retry=%s value=%s error=%s",
                        candidate.source,
                        candidate.kind,
                        retry_count,
                        candidate.value,
                        exc,
                    )
                else:
                    candidate.metadata["final_status"] = "failed"
                    self.store.append_failed_candidate(candidate)
                    logger.warning(
                        "candidate skipped source=%s kind=%s retry=%s value=%s error=%s",
                        candidate.source,
                        candidate.kind,
                        retry_count,
                        candidate.value,
                        exc,
                    )
                continue
            except Exception as exc:
                logger.exception(
                    "candidate failed source=%s kind=%s value=%s error=%s",
                    candidate.source,
                    candidate.kind,
                    candidate.value,
                    exc,
                )
                candidate.metadata["last_error"] = str(exc)
                candidate.metadata["final_status"] = "failed"
                self.store.append_failed_candidate(candidate)
                continue
            for entity in entities:
                matched = self.resolver.find_match(entity, seen_existing)
                if matched:
                    logger.info("dedupe merge incoming=%s matched=%s", entity.name, matched.id)
                    entity = self.resolver.merge(matched, entity)
                    seen_existing = [item for item in seen_existing if item.id != matched.id]
                entity.metadata = self.enrichment.enrich(entity)
                self.store.save_entity(entity)
                seen_existing.append(entity)
            logger.info(
                "candidate complete value=%s entities=%s new_candidates=%s processed=%s",
                candidate.value,
                len(entities),
                len(new_candidates),
                processed + 1,
            )
            for new_candidate in new_candidates:
                if new_candidate.depth <= self.max_depth:
                    heapq.heappush(queue, self._queue_item(new_candidate))
            processed += 1

        remaining.extend(item.candidate for item in queue)
        frontier = self._dedupe_candidates(remaining)
        self.store.save_frontier(frontier)
        self.coverage.snapshot(frontier)
        logger.info("crawl batch complete processed=%s remaining_frontier=%s", processed, len(frontier))

    def _run_parallel(self, max_candidates: int, workers: int) -> None:
        frontier = self.store.load_frontier()
        logger.info(
            "parallel crawl batch start frontier=%s max_candidates=%s workers=%s",
            len(frontier),
            max_candidates,
            workers,
        )
        queue = self._queue(frontier)
        processed = 0
        submitted = 0
        remaining: list[CrawlCandidate] = []
        seen_existing = list(self.store.iter_entities())
        futures: dict[Future[tuple[list[CityEntity], list[CrawlCandidate]]], CrawlCandidate] = {}
        enrichment_futures: dict[Future[list[CrawlCandidate]], str] = {}
        enriched_entity_ids: set[str] = set()
        enrichment_workers = max(1, min(2, workers))

        with (
            ThreadPoolExecutor(max_workers=workers, thread_name_prefix="crawler") as executor,
            ThreadPoolExecutor(max_workers=enrichment_workers, thread_name_prefix="enricher") as enrichment_executor,
        ):
            while (queue or futures or enrichment_futures) and processed < max_candidates:
                self._drain_enrichment_futures(enrichment_futures, queue)
                while queue and len(futures) < workers and submitted < max_candidates:
                    item = heapq.heappop(queue)
                    candidate = item.candidate
                    if candidate.depth > self.max_depth:
                        continue
                    adapter = self._adapter_for(candidate)
                    if adapter is None:
                        logger.warning(
                            "no adapter candidate source=%s kind=%s value=%s",
                            candidate.source,
                            candidate.kind,
                            candidate.value,
                        )
                        remaining.append(candidate)
                        continue
                    futures[executor.submit(self._crawl_candidate, adapter, candidate)] = candidate
                    submitted += 1

                if not futures:
                    if enrichment_futures:
                        self._drain_enrichment_futures(enrichment_futures, queue, block=True)
                        continue
                    break

                done, _ = wait(futures.keys(), return_when=FIRST_COMPLETED)
                for future in done:
                    candidate = futures.pop(future)
                    try:
                        entities, new_candidates = future.result()
                    except (httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError, PermissionError) as exc:
                        self._handle_retryable_failure(candidate, exc, remaining)
                        processed += 1
                        continue
                    except Exception as exc:
                        self._handle_unexpected_failure(candidate, exc)
                        processed += 1
                        continue

                    for entity in entities:
                        matched = self.resolver.find_match(entity, seen_existing)
                        if matched:
                            logger.info("dedupe merge incoming=%s matched=%s", entity.name, matched.id)
                            entity = self.resolver.merge(matched, entity)
                            seen_existing = [item for item in seen_existing if item.id != matched.id]
                        entity.metadata = self.enrichment.enrich(entity)
                        self.store.save_entity(entity)
                        seen_existing.append(entity)
                        if entity.id not in enriched_entity_ids:
                            enriched_entity_ids.add(entity.id)
                            enrichment_futures[
                                enrichment_executor.submit(self._production_enrichment_candidates, entity)
                            ] = entity.id
                    logger.info(
                        "candidate complete value=%s entities=%s new_candidates=%s processed=%s",
                        candidate.value,
                        len(entities),
                        len(new_candidates),
                        processed + 1,
                    )
                    for new_candidate in new_candidates:
                        if new_candidate.depth <= self.max_depth:
                            heapq.heappush(queue, self._queue_item(new_candidate))
                    processed += 1
                self._drain_enrichment_futures(enrichment_futures, queue)
            self._drain_enrichment_futures(enrichment_futures, queue, block=True)

        remaining.extend(item.candidate for item in queue)
        remaining.extend(futures.values())
        frontier = self._dedupe_candidates(remaining)
        self.store.save_frontier(frontier)
        self.coverage.snapshot(frontier)
        logger.info(
            "parallel crawl batch complete processed=%s submitted=%s remaining_frontier=%s",
            processed,
            submitted,
            len(frontier),
        )

    def _production_enrichment_candidates(self, entity: CityEntity) -> list[CrawlCandidate]:
        logger.info("background enrichment check entity_id=%s name=%s", entity.id, entity.name)
        candidates = self.validator.enrichment_candidates_for_entity(entity)
        logger.info(
            "background enrichment complete entity_id=%s name=%s candidates=%s",
            entity.id,
            entity.name,
            len(candidates),
        )
        return candidates

    def _drain_enrichment_futures(
        self,
        enrichment_futures: dict[Future[list[CrawlCandidate]], str],
        queue: list[QueueItem],
        block: bool = False,
    ) -> int:
        if not enrichment_futures:
            return 0
        if block:
            done, _ = wait(enrichment_futures.keys())
        else:
            done = {future for future in list(enrichment_futures) if future.done()}
        enqueued = 0
        for future in done:
            entity_id = enrichment_futures.pop(future)
            try:
                candidates = future.result()
            except Exception as exc:
                logger.exception("background enrichment failed entity_id=%s error=%s", entity_id, exc)
                continue
            accepted = 0
            for candidate in candidates:
                if candidate.depth <= self.max_depth:
                    heapq.heappush(queue, self._queue_item(candidate))
                    accepted += 1
            enqueued += accepted
            if candidates:
                logger.info(
                    "background enrichment enqueued entity_id=%s candidates=%s accepted=%s",
                    entity_id,
                    len(candidates),
                    accepted,
                )
        return enqueued

    def _crawl_candidate(
        self,
        adapter: SourceAdapter,
        candidate: CrawlCandidate,
    ) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        logger.info(
            "processing candidate source=%s kind=%s depth=%s priority=%.3f value=%s",
            candidate.source,
            candidate.kind,
            candidate.depth,
            candidate.priority,
            candidate.value,
        )
        return adapter.crawl(candidate)

    def _handle_retryable_failure(
        self,
        candidate: CrawlCandidate,
        exc: Exception,
        remaining: list[CrawlCandidate],
    ) -> None:
        candidate.metadata["last_error"] = str(exc)
        retry_count = int(candidate.metadata.get("retry_count", 0)) + 1
        candidate.metadata["retry_count"] = retry_count
        if self._should_retry_candidate(candidate, retry_count):
            candidate.priority *= 0.5
            remaining.append(candidate)
            logger.warning(
                "candidate deferred source=%s kind=%s retry=%s value=%s error=%s",
                candidate.source,
                candidate.kind,
                retry_count,
                candidate.value,
                exc,
            )
        else:
            candidate.metadata["final_status"] = "failed"
            self.store.append_failed_candidate(candidate)
            logger.warning(
                "candidate skipped source=%s kind=%s retry=%s value=%s error=%s",
                candidate.source,
                candidate.kind,
                retry_count,
                candidate.value,
                exc,
            )

    def _handle_unexpected_failure(self, candidate: CrawlCandidate, exc: Exception) -> None:
        logger.exception(
            "candidate failed source=%s kind=%s value=%s error=%s",
            candidate.source,
            candidate.kind,
            candidate.value,
            exc,
        )
        candidate.metadata["last_error"] = str(exc)
        candidate.metadata["final_status"] = "failed"
        self.store.append_failed_candidate(candidate)

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

    def _should_retry_candidate(self, candidate: CrawlCandidate, retry_count: int) -> bool:
        if candidate.kind == CandidateKind.SOURCE_URL:
            return retry_count < 1
        return retry_count < 3

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
