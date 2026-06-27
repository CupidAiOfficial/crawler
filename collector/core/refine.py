from __future__ import annotations

import logging

from collector.core.enrichment import EnrichmentEngine
from collector.core.entity_refinement import EntityRefiner
from collector.core.models import TextSignal
from collector.core.production_web_enrichment import ProductionWebEnricher
from collector.core.quality import ProductionReadinessValidator
from collector.core.storage import JsonStore
from collector.core.structured_extraction import StructuredExtractor


logger = logging.getLogger(__name__)


class RefinementPipeline:
    """Recomputes canonical enrichment after a crawl batch finishes."""

    def __init__(
        self,
        store: JsonStore,
        *,
        production_web_enrich: bool = False,
        max_web_enrich: int = 200,
        max_entities: int | None = None,
        fetch_open_images: bool = True,
    ) -> None:
        self.store = store
        self.enrichment = EnrichmentEngine()
        self.extractor = StructuredExtractor()
        self.validator = ProductionReadinessValidator()
        self.entity_refiner = EntityRefiner(fetch_open_images=fetch_open_images)
        self.web_enricher = ProductionWebEnricher() if production_web_enrich else None
        self.max_web_enrich = max(0, max_web_enrich)
        self.max_entities = max_entities

    def run(self) -> int:
        logger.info("refine start")
        count = 0
        web_enriched = 0
        for entity in list(self.store.iter_entities()):
            if self.max_entities is not None and count >= self.max_entities:
                logger.info("refine max_entities reached count=%s", count)
                break
            logger.info("refining entity id=%s name=%s", entity.id, entity.name)
            reviews = self._reviews(entity.id)
            entity.metadata = self.enrichment.enrich(entity, reviews)
            text = self._entity_text(entity, reviews)
            entity, extracted_reviews, relationships = self.extractor.enrich_from_document(
                entity,
                text=text,
                url=str(entity.website or ""),
            )
            if extracted_reviews:
                self.store.append_reviews(entity.id, extracted_reviews)
            if relationships:
                self.store.append_relationships(entity.id, relationships)
            entity = self.entity_refiner.refine(entity)
            if self.web_enricher and web_enriched < self.max_web_enrich:
                before = entity.model_dump(mode="json")
                entity = self.web_enricher.enrich_entity(entity)
                if entity.model_dump(mode="json") != before:
                    web_enriched += 1
                    entity = self.entity_refiner.refine(entity)
            quality = self.validator.validate(entity)
            if entity.status != "REJECTED_PAGE_FRAGMENT":
                entity.status = "ACTIVE" if quality.production_ready else entity.status
            entity.raw_json["production_quality"] = quality.as_dict()
            self.store.save_entity(entity)
            count += 1
        self.validator.validate_store(self.store)
        logger.info("refine complete entities=%s web_enriched=%s", count, web_enriched)
        return count

    def _reviews(self, entity_id: str) -> list[TextSignal]:
        raw = self.store._read_json(self.store.entity_dir(entity_id) / "reviews.json", [])  # noqa: SLF001
        reviews = []
        for item in raw:
            if isinstance(item, dict):
                try:
                    reviews.append(TextSignal.model_validate(item))
                except Exception:
                    logger.debug("skipping invalid review entity_id=%s payload=%s", entity_id, item)
        return reviews

    def _entity_text(self, entity, reviews: list[TextSignal]) -> str:
        return " ".join(
            str(part)
            for part in [
                entity.name,
                entity.display_name or "",
                entity.category,
                entity.primary_category or "",
                " ".join(entity.subcategories),
                entity.description or "",
                entity.summary or "",
                entity.locality or "",
                entity.address or "",
                " ".join(entity.amenities),
                " ".join(entity.metadata.intent_tags),
                " ".join(entity.metadata.context_keys),
                " ".join(entity.metadata.branch_ids),
                " ".join(review.text for review in reviews),
            ]
            if part
        )
