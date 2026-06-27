from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Iterable

from collector.core.config import settings
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate
from collector.core.storage import JsonStore


logger = logging.getLogger(__name__)


SOURCE_LIKE_NAME_PATTERNS = [
    r"\b(?:top|best)\s+\d+",
    r"^\d+\+?\s+(?:best|top|romantic|late night|mid night|cafes|restaurants|rooftop|hidden gems|places|peaceful)",
    r"\bplaces to visit\b",
    r"\bthings to do\b",
    r"\bweekend getaways?\b",
    r"\bhangout places\b",
    r"\bromantic places\b",
    r"\bcomplete guide\b",
    r"\bultimate checklist\b",
    r"\bwhere to\b",
    r"\bwhat'?s fuelling\b",
    r"\blist of\b",
]

BAD_EXACT_NAMES = {
    "right-triangle",
    "cross",
    "organic pages",
    "home",
    "hyderabad",
    "telangana",
    "about hyderabad",
    "about us",
    "terms & conditions",
    "terms and conditions",
    "privacy policy",
    "contact us",
    "blogs",
    "careers",
}

NON_SERVING_CATEGORIES = {
    "bus_station",
    "bus_stop",
    "police",
    "bank",
    "car",
    "clinic",
    "crossing",
    "cycleway",
    "footway",
    "health_post",
    "hospital",
    "kindergarten",
    "parking",
    "path",
    "primary",
    "primary_link",
    "residential",
    "road",
    "secondary",
    "secondary_link",
    "service",
    "tertiary",
    "tertiary_link",
    "ticket",
    "track",
    "trunk",
    "unclassified",
}

NON_SERVING_NAME_PATTERNS = [
    r"^address\s*:",
    r"^timings?\s*:",
    r"^entry fee\b",
    r"^skip to content$",
    r"^read now$",
    r"^view details$",
    r"^(?:add|apply|create|submit|register|sign\s*up|login|log\s*in|join|follow|share|save|view|read|learn)\b",
    r"^(?:why|what|how|where|when|who)\b",
    r"^request quote$",
    r"^upcoming community(?: trips)?$",
    r"\btour packages?\b",
    r"\btrips?\b",
    r"\broad\b",
    r"\broad\s+(?:no|number)\b",
    r"\brd\s*\d+\b",
    r"\bward\s+\d+\b",
    r"\bpolice station\b",
    r"\bbasthi dawakhana\b",
    r"\bprimary health centre\b",
    r"\bcheck\s*post\b",
    r"\bkaman\b",
    r"\b(?:logo|badge|primary image|contribution instructions|privacy policy|terms|cancellation policy|user profile)\b",
    r"\b(?:events|places|restaurants|cafes|clubs|pubs|activities|meetups|conferences|workshops|webinars)\s+in\s+hyderabad\b",
    r"^(?:tech|startup|business|ai|data|web|webinar|workshop|founder|investor)\s+(?:meetups?|events?|conferences?|summits?)$",
    r"^(?:free\s+)?(?:workshop|hackathon|rave party|party night|user group)$",
]

PRODUCTION_REQUIRED_FIELDS = {
    "name",
    "category",
    "locality",
    "address",
    "latitude",
    "longitude",
    "image",
    "source",
    "not_source_page",
}

SERVING_REQUIRED_FIELDS = {
    "name",
    "category",
    "locality",
    "address",
    "latitude",
    "longitude",
    "source",
    "not_source_page",
}


@dataclass
class EntityQualityResult:
    entity_id: str
    name: str
    production_ready: bool
    score: float
    missing_fields: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    enrichment_queries: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "entity_id": self.entity_id,
            "name": self.name,
            "production_ready": self.production_ready,
            "score": self.score,
            "missing_fields": self.missing_fields,
            "blockers": self.blockers,
            "warnings": self.warnings,
            "enrichment_queries": self.enrichment_queries,
        }


class ProductionReadinessValidator:
    """Validates whether crawler output is safe to serve in the app.

    The app card flow needs actual entities, not article/search pages. For a
    production serving row, geo is mandatory because nearby matching depends on it.
    Images, ratings, reviews, and timings improve score but are not hard blockers
    because many public/open sources do not legally expose them.
    """

    def validate(self, entity: CityEntity) -> EntityQualityResult:
        missing: list[str] = []
        blockers: list[str] = []
        warnings: list[str] = []

        if not entity.name:
            missing.append("name")
        if not entity.category:
            missing.append("category")
        if not entity.locality:
            missing.append("locality")
        if not entity.address:
            missing.append("address")
        if entity.latitude is None:
            missing.append("latitude")
        if entity.longitude is None:
            missing.append("longitude")
        if not entity.media and not entity.card.primary_image_url:
            missing.append("image")
        if not entity.sources:
            missing.append("source")

        if self.is_source_like(entity):
            blockers.append("source_or_article_title")
        if not self.is_recommendation_entity(entity):
            blockers.append("not_recommendation_entity")
        if entity.latitude is None or entity.longitude is None:
            blockers.append("missing_coordinates")
        if not entity.media and not entity.card.primary_image_url:
            blockers.append("missing_image")
        if not entity.locality:
            blockers.append("missing_locality")
        if not entity.address:
            blockers.append("missing_address")
        if not entity.sources:
            blockers.append("missing_source_provenance")
        if entity.confidence_score is not None and entity.confidence_score < 0.35:
            blockers.append("low_confidence")

        if entity.rating is None and not entity.ratings:
            warnings.append("missing_rating")
        if not entity.opening_hours_raw and not entity.timings:
            warnings.append("missing_timings")
        if not entity.metadata.intent_tags:
            warnings.append("missing_intent_tags")

        score = self._score(entity, missing, blockers, warnings)
        ready = not blockers and not (PRODUCTION_REQUIRED_FIELDS.intersection(missing))
        return EntityQualityResult(
            entity_id=entity.id,
            name=entity.name,
            production_ready=ready,
            score=score,
            missing_fields=sorted(set(missing)),
            blockers=sorted(set(blockers)),
            warnings=sorted(set(warnings)),
            enrichment_queries=self.enrichment_queries(entity, missing, blockers),
        )

    def serving_ready(self, entity: CityEntity, result: EntityQualityResult | None = None) -> bool:
        result = result or self.validate(entity)
        refinement = entity.raw_json.get("refinement") if isinstance(entity.raw_json, dict) else None
        if isinstance(refinement, dict) and refinement.get("decision") == "reject_page_fragment":
            return False
        if entity.status == "REJECTED_PAGE_FRAGMENT":
            return False
        if not self.is_recommendation_entity(entity) or self.is_source_like(entity):
            return False
        if SERVING_REQUIRED_FIELDS.intersection(result.missing_fields):
            return False
        if "low_confidence" in result.blockers:
            return False
        return True

    def validate_store(self, store: JsonStore) -> list[EntityQualityResult]:
        results = [self.validate(entity) for entity in store.iter_entities()]
        report = {
            "total": len(results),
            "production_ready": sum(1 for result in results if result.production_ready),
            "blocked": sum(1 for result in results if not result.production_ready),
            "results": [result.as_dict() for result in results],
        }
        store.write_index("production_validation.json", report)
        logger.info(
            "production validation complete total=%s ready=%s blocked=%s",
            report["total"],
            report["production_ready"],
            report["blocked"],
        )
        return results

    def enqueue_enrichment(self, store: JsonStore, max_entities: int = 200) -> int:
        existing = store.load_frontier()
        new_candidates: list[CrawlCandidate] = []
        for entity in store.iter_entities():
            result = self.validate(entity)
            if result.production_ready or not result.enrichment_queries:
                continue
            if not self.should_enqueue_enrichment(entity, result):
                continue
            new_candidates.extend(self.enrichment_candidates_for_entity(entity, result))
            if len(new_candidates) >= max_entities * 3:
                break
        if not new_candidates:
            logger.info("production enrichment enqueue skipped no candidates")
            return 0
        store.save_frontier(existing + new_candidates)
        logger.info("production enrichment enqueued candidates=%s", len(new_candidates))
        return len(new_candidates)

    def enrichment_candidates_for_entity(
        self,
        entity: CityEntity,
        result: EntityQualityResult | None = None,
    ) -> list[CrawlCandidate]:
        result = result or self.validate(entity)
        if result.production_ready or not result.enrichment_queries:
            return []
        if not self.should_enqueue_enrichment(entity, result):
            logger.info(
                "entity enrichment skipped entity_id=%s name=%s blockers=%s",
                entity.id,
                entity.name,
                ",".join(result.blockers),
            )
            return []
        candidates: list[CrawlCandidate] = []
        for query in result.enrichment_queries:
            candidates.extend(self._candidates_for_query(entity, query))
        logger.info(
            "entity enrichment candidates entity_id=%s name=%s missing=%s blockers=%s candidates=%s",
            entity.id,
            entity.name,
            ",".join(result.missing_fields),
            ",".join(result.blockers),
            len(candidates),
        )
        return candidates

    def should_enqueue_enrichment(self, entity: CityEntity, result: EntityQualityResult | None = None) -> bool:
        result = result or self.validate(entity)
        refinement = entity.raw_json.get("refinement") if isinstance(entity.raw_json, dict) else None
        if isinstance(refinement, dict) and refinement.get("decision") == "reject_page_fragment":
            return False
        if entity.status == "REJECTED_PAGE_FRAGMENT":
            return False
        if self.is_source_like(entity) or not self.is_recommendation_entity(entity):
            return False
        hard_blockers = {"not_recommendation_entity", "source_or_article_title", "low_confidence"}
        if hard_blockers.intersection(result.blockers):
            return False
        return bool(result.enrichment_queries)

    def enrichment_queries(self, entity: CityEntity, missing: Iterable[str], blockers: Iterable[str]) -> list[str]:
        if "source_or_article_title" in blockers or "not_recommendation_entity" in blockers:
            return []
        missing_set = set(missing)
        queries: list[str] = []
        base = self._clean_query_name(entity.name)
        locality = entity.locality or "Hyderabad"
        if {"latitude", "longitude"}.intersection(missing_set):
            queries.append(f"{base} {locality} exact location")
            queries.append(f"{base} {locality} address")
        if "address" in missing_set or not entity.address:
            queries.append(f"{base} {locality} address timings")
        if "image" in missing_set:
            queries.append(f"{base} {locality} photos")
            queries.append(f"{base} {locality} images")
        return self._unique(queries)

    def is_source_like(self, entity: CityEntity) -> bool:
        name = (entity.name or "").strip().lower()
        if name in BAD_EXACT_NAMES:
            return True
        if entity.raw_json.get("extraction_mode") == "mentioned_entity":
            return False
        if entity.latitude is not None and entity.longitude is not None:
            return False
        return any(re.search(pattern, name, flags=re.I) for pattern in SOURCE_LIKE_NAME_PATTERNS)

    def is_recommendation_entity(self, entity: CityEntity) -> bool:
        name = (entity.name or "").strip().lower()
        category = (entity.primary_category or entity.category or "").strip().lower()
        if entity.status == "REJECTED_PAGE_FRAGMENT":
            return False
        if not name or not category:
            return False
        if category in NON_SERVING_CATEGORIES:
            return False
        if " hyderabad" in category and category not in {"hyderabad", "secunderabad"}:
            return False
        if any(re.search(pattern, name, flags=re.I) for pattern in NON_SERVING_NAME_PATTERNS):
            return False
        if name in {"linkedin", "telegram", "whatsapp", "messenger", "android", "pinterest", "more", "skip to content"}:
            return False
        return True

    def _candidates_for_query(self, entity: CityEntity, query: str) -> list[CrawlCandidate]:
        metadata = {
            "entity_id": entity.id,
            "entity_name": entity.name,
            "reason": "production_validation_enrichment",
        }
        candidates = [
            CrawlCandidate(
                kind=CandidateKind.QUERY,
                source="openstreetmap",
                value=query,
                priority=0.9,
                depth=0,
                metadata=metadata.copy(),
            ),
            CrawlCandidate(
                kind=CandidateKind.QUERY,
                source="firecrawl_search",
                value=query,
                priority=0.7,
                depth=0,
                metadata=metadata.copy(),
            ),
        ]
        if settings.google_custom_search_api_key and settings.google_custom_search_engine_id:
            candidates.append(
                CrawlCandidate(
                    kind=CandidateKind.QUERY,
                    source="google_search",
                    value=query,
                    priority=0.8,
                    depth=0,
                    metadata=metadata.copy(),
                )
            )
        return candidates

    def _score(
        self,
        entity: CityEntity,
        missing: list[str],
        blockers: list[str],
        warnings: list[str],
    ) -> float:
        score = 1.0
        score -= len(set(missing).intersection(PRODUCTION_REQUIRED_FIELDS)) * 0.18
        score -= len(blockers) * 0.25
        score -= len(warnings) * 0.04
        if entity.address:
            score += 0.05
        if entity.media or entity.card.primary_image_url:
            score += 0.05
        if entity.rating is not None or entity.ratings:
            score += 0.05
        if entity.metadata.intent_tags:
            score += 0.05
        return round(max(0.0, min(1.0, score)), 3)

    def _clean_query_name(self, name: str) -> str:
        value = re.sub(r"\s*[-|:]\s*(?:Review|Timings|Entry Fee|Hyderabad).*$", "", name, flags=re.I)
        return re.sub(r"\s+", " ", value).strip()

    def _unique(self, values: list[str]) -> list[str]:
        out: list[str] = []
        seen: set[str] = set()
        for value in values:
            key = value.lower()
            if key and key not in seen:
                out.append(value)
                seen.add(key)
        return out
