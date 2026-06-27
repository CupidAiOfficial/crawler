from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

from collector.core.image_blob import DownloadedImage, ImageBlobDownloader
from collector.core.models import CityEntity
from collector.core.quality import ProductionReadinessValidator
from collector.core.storage import JsonStore


logger = logging.getLogger(__name__)


class PostgresExportError(RuntimeError):
    pass


class PostgresServingWriter:
    """Writes canonical crawler entities into app-serving PostgreSQL tables."""

    def __init__(self, database_url: str) -> None:
        try:
            import psycopg  # type: ignore
        except ImportError as exc:
            raise PostgresExportError(
                "Postgres export requires psycopg. Install with: python -m pip install 'psycopg[binary]>=3.2.0'"
            ) from exc
        self.psycopg = psycopg
        self.database_url = database_url
        self.validator = ProductionReadinessValidator()
        self.image_downloader = ImageBlobDownloader()

    def export_store(self, store: JsonStore) -> int:
        logger.info("postgres export start")
        count = 0
        with self.psycopg.connect(self.database_url) as conn:
            self.ensure_schema(conn)
            quality_results = {result.entity_id: result for result in self.validator.validate_store(store)}
            self.export_raw_entities(conn, store, quality_results)
            self.export_quality_results(conn, quality_results.values())
            exported_ids: list[str] = []
            exported_keys: set[str] = set()
            for entity in store.iter_entities():
                quality = quality_results[entity.id]
                if not quality.production_ready or not self.validator.serving_ready(entity, quality):
                    logger.info(
                        "postgres skip non-serving entity id=%s name=%s blockers=%s missing=%s",
                        entity.id,
                        entity.name,
                        quality.blockers,
                        quality.missing_fields,
                    )
                    continue
                dedupe_key = self._serving_dedupe_key(entity)
                if dedupe_key in exported_keys:
                    logger.info(
                        "postgres skip duplicate serving entity id=%s name=%s key=%s",
                        entity.id,
                        entity.name,
                        dedupe_key,
                    )
                    continue
                exported_keys.add(dedupe_key)
                exported = self.upsert_entity(conn, entity, store, quality.production_ready)
                if not exported:
                    logger.info("postgres skip serving entity no blob image id=%s name=%s", entity.id, entity.name)
                    continue
                exported_ids.append(entity.id)
                count += 1
            self.cleanup_non_production_entities(conn, exported_ids)
            self.export_source_pages(conn, store)
            conn.commit()
        logger.info("postgres export complete entities=%s", count)
        return count

    def _serving_dedupe_key(self, entity: CityEntity) -> str:
        name = (entity.name or "").lower()
        name = name.replace("golkonda", "golconda")
        name = re.sub(r"[^a-z0-9]+", " ", name)
        name = re.sub(r"\b(?:the|near|at|in|hyderabad|telangana|india)\b", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if entity.latitude is not None and entity.longitude is not None:
            return f"{name}|{round(float(entity.latitude), 4)}:{round(float(entity.longitude), 4)}"
        return name

    def ensure_schema(self, conn: Any) -> None:
        logger.info("postgres ensure schema")
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crawler_entities_raw (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    entity_type TEXT,
                    primary_category TEXT,
                    locality TEXT,
                    address TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    website TEXT,
                    has_media BOOLEAN DEFAULT false,
                    production_ready BOOLEAN DEFAULT false,
                    quality_score DOUBLE PRECISION DEFAULT 0,
                    quality_blockers TEXT[] DEFAULT '{}',
                    quality_missing_fields TEXT[] DEFAULT '{}',
                    raw_json JSONB DEFAULT '{}'::jsonb,
                    first_seen_at TIMESTAMPTZ,
                    last_seen_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS city_entities (
                    id TEXT PRIMARY KEY,
                    canonical_name TEXT NOT NULL,
                    display_name TEXT,
                    entity_type TEXT NOT NULL,
                    primary_category TEXT,
                    secondary_categories TEXT[] DEFAULT '{}',
                    context_keys TEXT[] DEFAULT '{}',
                    branch_ids TEXT[] DEFAULT '{}',
                    intent_tags TEXT[] DEFAULT '{}',
                    negative_intent_tags TEXT[] DEFAULT '{}',
                    description TEXT,
                    summary TEXT,
                    locality TEXT,
                    address TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    geo_precision TEXT,
                    website TEXT,
                    phone_numbers TEXT[] DEFAULT '{}',
                    emails TEXT[] DEFAULT '{}',
                    price_level TEXT,
                    rating DOUBLE PRECISION,
                    rating_count INTEGER,
                    review_count INTEGER,
                    popularity_score DOUBLE PRECISION,
                    hidden_gem_score DOUBLE PRECISION,
                    confidence_score DOUBLE PRECISION,
                    source_count INTEGER,
                    status TEXT DEFAULT 'ACTIVE',
                    primary_image_url TEXT,
                    primary_image_id TEXT,
                    card_json JSONB DEFAULT '{}'::jsonb,
                    raw_json JSONB DEFAULT '{}'::jsonb,
                    first_seen_at TIMESTAMPTZ,
                    last_seen_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute("ALTER TABLE city_entities ADD COLUMN IF NOT EXISTS primary_image_id TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_intents (
                    entity_id TEXT REFERENCES city_entities(id) ON DELETE CASCADE,
                    intent_tag TEXT NOT NULL,
                    score DOUBLE PRECISION DEFAULT 0,
                    evidence TEXT,
                    PRIMARY KEY (entity_id, intent_tag)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_sources (
                    source_key TEXT PRIMARY KEY,
                    entity_id TEXT REFERENCES city_entities(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    source_url TEXT,
                    source_type TEXT,
                    raw_path TEXT,
                    fetched_at TIMESTAMPTZ,
                    extraction_confidence DOUBLE PRECISION,
                    raw_json JSONB DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_reviews (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT REFERENCES city_entities(id) ON DELETE CASCADE,
                    source TEXT,
                    author_hash TEXT,
                    rating DOUBLE PRECISION,
                    text TEXT,
                    sentiment TEXT,
                    sentiment_score DOUBLE PRECISION,
                    topics TEXT[] DEFAULT '{}',
                    pros TEXT[] DEFAULT '{}',
                    cons TEXT[] DEFAULT '{}',
                    observed_at TIMESTAMPTZ,
                    raw_json JSONB DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_media (
                    id TEXT PRIMARY KEY,
                    entity_id TEXT REFERENCES city_entities(id) ON DELETE CASCADE,
                    media_type TEXT,
                    url TEXT,
                    source_url TEXT,
                    image_blob BYTEA,
                    mime_type TEXT,
                    byte_size INTEGER,
                    content_hash TEXT,
                    local_path TEXT,
                    caption TEXT,
                    labels TEXT[] DEFAULT '{}',
                    source TEXT,
                    is_primary BOOLEAN DEFAULT false,
                    raw_json JSONB DEFAULT '{}'::jsonb
                )
                """
            )
            cur.execute("ALTER TABLE entity_media ADD COLUMN IF NOT EXISTS source_url TEXT")
            cur.execute("ALTER TABLE entity_media ADD COLUMN IF NOT EXISTS image_blob BYTEA")
            cur.execute("ALTER TABLE entity_media ADD COLUMN IF NOT EXISTS mime_type TEXT")
            cur.execute("ALTER TABLE entity_media ADD COLUMN IF NOT EXISTS byte_size INTEGER")
            cur.execute("ALTER TABLE entity_media ADD COLUMN IF NOT EXISTS content_hash TEXT")
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS entity_relationships (
                    from_entity_id TEXT REFERENCES city_entities(id) ON DELETE CASCADE,
                    relation_type TEXT NOT NULL,
                    to_entity_id TEXT NOT NULL,
                    target_name TEXT,
                    weight DOUBLE PRECISION DEFAULT 0,
                    evidence TEXT,
                    source_url TEXT,
                    raw_json JSONB DEFAULT '{}'::jsonb,
                    PRIMARY KEY (from_entity_id, relation_type, to_entity_id)
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crawler_source_pages (
                    url TEXT PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    source TEXT,
                    source_type TEXT,
                    raw_path TEXT,
                    is_source_page BOOLEAN DEFAULT true,
                    search_query TEXT,
                    search_rank INTEGER,
                    raw_json JSONB DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS crawler_entity_quality (
                    entity_id TEXT PRIMARY KEY,
                    name TEXT,
                    production_ready BOOLEAN NOT NULL,
                    score DOUBLE PRECISION DEFAULT 0,
                    missing_fields TEXT[] DEFAULT '{}',
                    blockers TEXT[] DEFAULT '{}',
                    warnings TEXT[] DEFAULT '{}',
                    enrichment_queries TEXT[] DEFAULT '{}',
                    raw_json JSONB DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ DEFAULT now()
                )
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_city_entities_context ON city_entities USING gin(context_keys)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawler_entities_raw_ready ON crawler_entities_raw(production_ready)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawler_entities_raw_name ON crawler_entities_raw(canonical_name)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_city_entities_branch ON city_entities USING gin(branch_ids)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_city_entities_intents ON city_entities USING gin(intent_tags)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entity_intents_tag ON entity_intents(intent_tag)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entity_media_entity ON entity_media(entity_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_entity_media_hash ON entity_media(content_hash)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawler_source_pages_source_type ON crawler_source_pages(source_type)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_crawler_entity_quality_ready ON crawler_entity_quality(production_ready)")

    def export_raw_entities(self, conn: Any, store: JsonStore, quality_results: dict[str, Any]) -> int:
        count = 0
        with conn.cursor() as cur:
            for entity in store.iter_entities():
                quality = quality_results.get(entity.id)
                cur.execute(
                    """
                    INSERT INTO crawler_entities_raw(
                        id, canonical_name, entity_type, primary_category, locality, address,
                        latitude, longitude, website, has_media, production_ready, quality_score,
                        quality_blockers, quality_missing_fields, raw_json, first_seen_at, last_seen_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        canonical_name = EXCLUDED.canonical_name,
                        entity_type = EXCLUDED.entity_type,
                        primary_category = EXCLUDED.primary_category,
                        locality = EXCLUDED.locality,
                        address = EXCLUDED.address,
                        latitude = EXCLUDED.latitude,
                        longitude = EXCLUDED.longitude,
                        website = EXCLUDED.website,
                        has_media = EXCLUDED.has_media,
                        production_ready = EXCLUDED.production_ready,
                        quality_score = EXCLUDED.quality_score,
                        quality_blockers = EXCLUDED.quality_blockers,
                        quality_missing_fields = EXCLUDED.quality_missing_fields,
                        raw_json = EXCLUDED.raw_json,
                        last_seen_at = EXCLUDED.last_seen_at,
                        updated_at = now()
                    """,
                    (
                        entity.id,
                        entity.name,
                        entity.entity_type,
                        entity.primary_category or entity.category,
                        entity.locality,
                        entity.address,
                        entity.latitude,
                        entity.longitude,
                        str(entity.website) if entity.website else None,
                        bool(entity.media or entity.card.primary_image_url),
                        bool(quality.production_ready) if quality else False,
                        quality.score if quality else 0,
                        quality.blockers if quality else [],
                        quality.missing_fields if quality else [],
                        json.dumps(entity.model_dump(mode="json")),
                        entity.first_seen_at,
                        entity.last_seen_at,
                    ),
                )
                count += 1
        logger.info("postgres raw entity export complete rows=%s", count)
        return count

    def cleanup_non_production_entities(self, conn: Any, ready_ids: list[str]) -> int:
        with conn.cursor() as cur:
            if ready_ids:
                cur.execute("DELETE FROM city_entities WHERE NOT (id = ANY(%s))", (ready_ids,))
            else:
                cur.execute("DELETE FROM city_entities")
            deleted = cur.rowcount or 0
        if deleted:
            logger.info("postgres cleanup removed non-production serving entities=%s", deleted)
        return deleted

    def export_quality_results(self, conn: Any, results: Any) -> int:
        count = 0
        with conn.cursor() as cur:
            for result in results:
                cur.execute(
                    """
                    INSERT INTO crawler_entity_quality(
                        entity_id, name, production_ready, score, missing_fields,
                        blockers, warnings, enrichment_queries, raw_json, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (entity_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        production_ready = EXCLUDED.production_ready,
                        score = EXCLUDED.score,
                        missing_fields = EXCLUDED.missing_fields,
                        blockers = EXCLUDED.blockers,
                        warnings = EXCLUDED.warnings,
                        enrichment_queries = EXCLUDED.enrichment_queries,
                        raw_json = EXCLUDED.raw_json,
                        updated_at = now()
                    """,
                    (
                        result.entity_id,
                        result.name,
                        result.production_ready,
                        result.score,
                        result.missing_fields,
                        result.blockers,
                        result.warnings,
                        result.enrichment_queries,
                        json.dumps(result.as_dict()),
                    ),
                )
                count += 1
        logger.info("postgres quality export complete rows=%s", count)
        return count

    def export_source_pages(self, conn: Any, store: JsonStore) -> int:
        count = 0
        with conn.cursor() as cur:
            for page in store.iter_source_pages():
                url = page.get("url")
                if not isinstance(url, str) or not url:
                    continue
                cur.execute(
                    """
                    INSERT INTO crawler_source_pages(
                        url, title, description, source, source_type, raw_path,
                        is_source_page, search_query, search_rank, raw_json, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    ON CONFLICT (url) DO UPDATE SET
                        title = EXCLUDED.title,
                        description = EXCLUDED.description,
                        source = EXCLUDED.source,
                        source_type = EXCLUDED.source_type,
                        raw_path = EXCLUDED.raw_path,
                        is_source_page = EXCLUDED.is_source_page,
                        search_query = EXCLUDED.search_query,
                        search_rank = EXCLUDED.search_rank,
                        raw_json = EXCLUDED.raw_json,
                        updated_at = now()
                    """,
                    (
                        url,
                        page.get("title"),
                        page.get("description"),
                        page.get("source"),
                        page.get("source_type"),
                        page.get("raw_path"),
                        bool(page.get("is_source_page", True)),
                        page.get("search_query"),
                        page.get("search_rank"),
                        json.dumps(page),
                    ),
                )
                count += 1
        logger.info("postgres source page export complete pages=%s", count)
        return count

    def upsert_entity(self, conn: Any, entity: CityEntity, store: JsonStore, production_ready: bool = False) -> bool:
        logger.info("postgres upsert entity id=%s name=%s", entity.id, entity.name)
        phone_numbers = entity.contact.get("phone_numbers") or entity.contact.get("phone") or []
        if isinstance(phone_numbers, str):
            phone_numbers = [phone_numbers]
        emails = entity.contact.get("emails") or entity.contact.get("email") or []
        if isinstance(emails, str):
            emails = [emails]
        image_payloads = self._download_entity_images(entity, store)
        if not image_payloads:
            logger.info("postgres serving rejected missing_downloaded_image_blob id=%s name=%s", entity.id, entity.name)
            return False
        primary_image_id = image_payloads[0][0] if image_payloads else None
        entity.card.primary_image_id = primary_image_id
        entity.card.primary_image_url = None
        with conn.cursor() as cur:
            self.delete_entity_children(cur, entity.id)
            cur.execute(
                """
                INSERT INTO city_entities (
                    id, canonical_name, display_name, entity_type, primary_category,
                    secondary_categories, context_keys, branch_ids, intent_tags, negative_intent_tags,
                    description, summary, locality, address, latitude, longitude, geo_precision,
                    website, phone_numbers, emails, price_level, rating, rating_count, review_count,
                    popularity_score, hidden_gem_score, confidence_score, source_count, status,
                    primary_image_url, primary_image_id, card_json, raw_json, first_seen_at, last_seen_at, updated_at
                )
                VALUES (
                    %(id)s, %(canonical_name)s, %(display_name)s, %(entity_type)s, %(primary_category)s,
                    %(secondary_categories)s, %(context_keys)s, %(branch_ids)s, %(intent_tags)s, %(negative_intent_tags)s,
                    %(description)s, %(summary)s, %(locality)s, %(address)s, %(latitude)s, %(longitude)s, %(geo_precision)s,
                    %(website)s, %(phone_numbers)s, %(emails)s, %(price_level)s, %(rating)s, %(rating_count)s, %(review_count)s,
                    %(popularity_score)s, %(hidden_gem_score)s, %(confidence_score)s, %(source_count)s, %(status)s,
                    %(primary_image_url)s, %(primary_image_id)s, %(card_json)s::jsonb, %(raw_json)s::jsonb, %(first_seen_at)s, %(last_seen_at)s, now()
                )
                ON CONFLICT (id) DO UPDATE SET
                    canonical_name = EXCLUDED.canonical_name,
                    display_name = EXCLUDED.display_name,
                    entity_type = EXCLUDED.entity_type,
                    primary_category = EXCLUDED.primary_category,
                    secondary_categories = EXCLUDED.secondary_categories,
                    context_keys = EXCLUDED.context_keys,
                    branch_ids = EXCLUDED.branch_ids,
                    intent_tags = EXCLUDED.intent_tags,
                    negative_intent_tags = EXCLUDED.negative_intent_tags,
                    description = EXCLUDED.description,
                    summary = EXCLUDED.summary,
                    locality = EXCLUDED.locality,
                    address = EXCLUDED.address,
                    latitude = EXCLUDED.latitude,
                    longitude = EXCLUDED.longitude,
                    geo_precision = EXCLUDED.geo_precision,
                    website = EXCLUDED.website,
                    phone_numbers = EXCLUDED.phone_numbers,
                    emails = EXCLUDED.emails,
                    price_level = EXCLUDED.price_level,
                    rating = EXCLUDED.rating,
                    rating_count = EXCLUDED.rating_count,
                    review_count = EXCLUDED.review_count,
                    popularity_score = EXCLUDED.popularity_score,
                    hidden_gem_score = EXCLUDED.hidden_gem_score,
                    confidence_score = EXCLUDED.confidence_score,
                    source_count = EXCLUDED.source_count,
                    status = EXCLUDED.status,
                    primary_image_url = EXCLUDED.primary_image_url,
                    primary_image_id = EXCLUDED.primary_image_id,
                    card_json = EXCLUDED.card_json,
                    raw_json = EXCLUDED.raw_json,
                    last_seen_at = EXCLUDED.last_seen_at,
                    updated_at = now()
                """,
                {
                    "id": entity.id,
                    "canonical_name": entity.name,
                    "display_name": entity.display_name,
                    "entity_type": entity.entity_type,
                    "primary_category": entity.primary_category or entity.category,
                    "secondary_categories": entity.subcategories,
                    "context_keys": entity.metadata.context_keys,
                    "branch_ids": entity.metadata.branch_ids,
                    "intent_tags": entity.metadata.intent_tags,
                    "negative_intent_tags": entity.metadata.negative_intent_tags,
                    "description": entity.description,
                    "summary": entity.summary,
                    "locality": entity.locality,
                    "address": entity.address,
                    "latitude": entity.latitude,
                    "longitude": entity.longitude,
                    "geo_precision": entity.geo_precision,
                    "website": str(entity.website) if entity.website else None,
                    "phone_numbers": phone_numbers,
                    "emails": emails,
                    "price_level": entity.price_level,
                    "rating": entity.rating,
                    "rating_count": entity.rating_count,
                    "review_count": entity.review_count,
                    "popularity_score": entity.metadata.popularity_score,
                    "hidden_gem_score": entity.metadata.hidden_gem_score,
                    "confidence_score": entity.confidence_score,
                    "source_count": entity.source_count or len(entity.sources),
                    "status": "PRODUCTION_READY" if production_ready else "NEEDS_ENRICHMENT",
                    "primary_image_url": None,
                    "primary_image_id": primary_image_id,
                    "card_json": json.dumps(entity.card.model_dump(mode="json")),
                    "raw_json": json.dumps(entity.model_dump(mode="json")),
                    "first_seen_at": entity.first_seen_at,
                    "last_seen_at": entity.last_seen_at,
                },
            )
            for tag, score in entity.metadata.suitability_scores.items():
                cur.execute(
                    """
                    INSERT INTO entity_intents(entity_id, intent_tag, score, evidence)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (entity_id, intent_tag) DO UPDATE
                    SET score = EXCLUDED.score, evidence = EXCLUDED.evidence
                    """,
                    (entity.id, tag, score, "crawler suitability score"),
                )
            for tag in entity.metadata.intent_tags:
                cur.execute(
                    """
                    INSERT INTO entity_intents(entity_id, intent_tag, score, evidence)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (entity_id, intent_tag) DO NOTHING
                    """,
                    (entity.id, tag, entity.metadata.suitability_scores.get(tag, 0.5), "crawler intent tag"),
                )
            for source in entity.sources:
                source_key = f"{entity.id}:{source.source}:{source.url or ''}:{source.raw_path or ''}"
                cur.execute(
                    """
                    INSERT INTO entity_sources(source_key, entity_id, source, source_url, source_type, raw_path, fetched_at, extraction_confidence, raw_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (source_key) DO UPDATE SET
                        fetched_at = EXCLUDED.fetched_at,
                        extraction_confidence = EXCLUDED.extraction_confidence,
                        raw_json = EXCLUDED.raw_json
                    """,
                    (
                        source_key,
                        entity.id,
                        source.source,
                        source.url,
                        source.source_type,
                        source.raw_path,
                        source.fetched_at,
                        source.extraction_confidence,
                        json.dumps(source.model_dump(mode="json")),
                    ),
                )
            for asset_id, asset, image in image_payloads:
                cur.execute(
                    """
                    INSERT INTO entity_media(
                        id, entity_id, media_type, url, source_url, image_blob, mime_type, byte_size,
                        content_hash, local_path, caption, labels, source, is_primary, raw_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        image_blob = EXCLUDED.image_blob,
                        mime_type = EXCLUDED.mime_type,
                        byte_size = EXCLUDED.byte_size,
                        content_hash = EXCLUDED.content_hash,
                        labels = EXCLUDED.labels,
                        caption = EXCLUDED.caption,
                        is_primary = EXCLUDED.is_primary,
                        raw_json = EXCLUDED.raw_json
                    """,
                    (
                        asset.id,
                        entity.id,
                        asset.kind,
                        None,
                        asset.url,
                        self.psycopg.Binary(image.data),
                        image.mime_type,
                        image.byte_size,
                        image.content_hash,
                        asset.local_path,
                        asset.caption,
                        asset.labels,
                        asset.source,
                        asset.is_primary,
                        json.dumps(asset.model_dump(mode="json")),
                    ),
                )
            for rel in entity.relationships:
                cur.execute(
                    """
                    INSERT INTO entity_relationships(from_entity_id, relation_type, to_entity_id, target_name, weight, evidence, source_url, raw_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (from_entity_id, relation_type, to_entity_id) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        evidence = EXCLUDED.evidence,
                        source_url = EXCLUDED.source_url,
                        raw_json = EXCLUDED.raw_json
                    """,
                    (
                        entity.id,
                        rel.predicate,
                        rel.object_id,
                        rel.object_name,
                        rel.confidence,
                        rel.evidence,
                        rel.source_url,
                        json.dumps(rel.model_dump(mode="json")),
                    ),
                )
            reviews = store._read_json(store.entity_dir(entity.id) / "reviews.json", [])  # noqa: SLF001
            for review in reviews:
                if not isinstance(review, dict) or not review.get("id"):
                    continue
                cur.execute(
                    """
                    INSERT INTO entity_reviews(id, entity_id, source, author_hash, rating, text, sentiment, sentiment_score, topics, pros, cons, observed_at, raw_json)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    ON CONFLICT (id) DO UPDATE SET
                        text = EXCLUDED.text,
                        sentiment = EXCLUDED.sentiment,
                        sentiment_score = EXCLUDED.sentiment_score,
                        topics = EXCLUDED.topics,
                        raw_json = EXCLUDED.raw_json
                    """,
                    (
                        review["id"],
                        entity.id,
                        review.get("source"),
                        review.get("author_hash"),
                        review.get("rating"),
                        review.get("text"),
                        review.get("sentiment"),
                        review.get("sentiment_score"),
                        review.get("topics") or [],
                        review.get("pros") or [],
                        review.get("cons") or [],
                        review.get("created_at") or review.get("fetched_at"),
                        json.dumps(review),
                    ),
                )
        return True

    def delete_entity_children(self, cur: Any, entity_id: str) -> None:
        cur.execute("DELETE FROM entity_intents WHERE entity_id = %s", (entity_id,))
        cur.execute("DELETE FROM entity_sources WHERE entity_id = %s", (entity_id,))
        cur.execute("DELETE FROM entity_media WHERE entity_id = %s", (entity_id,))
        cur.execute("DELETE FROM entity_reviews WHERE entity_id = %s", (entity_id,))
        cur.execute("DELETE FROM entity_relationships WHERE from_entity_id = %s", (entity_id,))

    def _download_entity_images(self, entity: CityEntity, store: JsonStore) -> list[tuple[str, Any, DownloadedImage]]:
        assets = list(entity.media)
        if entity.card.primary_image_url and all(asset.url != entity.card.primary_image_url for asset in assets):
            from collector.core.models import MediaAsset

            assets.insert(
                0,
                MediaAsset(
                    id=f"{entity.id}:card-primary-image",
                    source="card",
                    url=entity.card.primary_image_url,
                    kind="image",
                    is_primary=True,
                    copyright_risk="unknown",
                ),
            )
        out: list[tuple[str, Any, DownloadedImage]] = []
        seen_hashes: set[str] = set()
        for index, asset in enumerate(assets[:8]):
            image = self._load_local_image(asset, store) if asset.local_path else self.image_downloader.download(asset.url)
            if image is None or image.content_hash in seen_hashes:
                continue
            seen_hashes.add(image.content_hash)
            asset.content_hash = image.content_hash
            asset.mime_type = image.mime_type
            asset.byte_size = image.byte_size
            asset.is_primary = asset.is_primary or index == 0
            raw_asset_id = asset.id or image.content_hash[:20]
            asset_id = raw_asset_id if raw_asset_id.startswith(f"{entity.id}:") else f"{entity.id}:{raw_asset_id}"
            asset.id = asset_id
            out.append((asset_id, asset, image))
        return out

    def _load_local_image(self, asset: Any, store: JsonStore) -> DownloadedImage | None:
        path = Path(str(asset.local_path or ""))
        if not path.is_absolute():
            path = store.data_root / path
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("local image read failed path=%s error=%s", path, exc)
            return None
        if not data:
            return None
        suffix = path.suffix.lower()
        mime_type = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
            ".gif": "image/gif",
            ".svg": "image/svg+xml",
        }.get(suffix, asset.mime_type or "application/octet-stream")
        if not str(mime_type).startswith("image/"):
            return None
        return DownloadedImage(
            url=str(asset.url or path),
            data=data,
            mime_type=str(mime_type),
            content_hash=hashlib.sha256(data).hexdigest(),
        )
