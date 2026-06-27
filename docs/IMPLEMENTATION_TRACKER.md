# Crawler Extraction Implementation Tracker

## Implemented In This Pass

- Expanded `CityEntity` and child models to cover the final extraction standard:
  identity, source metadata, location, contact, timings, ratings, raw text evidence,
  intent tags, context keys, branch IDs, atmosphere, amenities, pricing, event details,
  community details, media, relationships, dedupe signals, safety/trust signals,
  app card fields, and extraction audit.
- Added `StructuredExtractor` for public HTML/markdown/text extraction:
  contacts, locality, timings, pricing, ratings, context keys, branch IDs, intent tags,
  negative tags, atmosphere, amenities, media metadata, event/community hints,
  review-like text signals, relationships, dedupe fingerprints, app cards, and field evidence.
- Wired structured extraction into:
  - OpenStreetMap entities
  - generic web pages
  - Firecrawl search/page extraction
  - Wikipedia search entities
  - Wikidata search entities
- Added sidecar storage for:
  - `source_snapshots.json`
  - `reviews.json`
  - `comments.json`
  - `relationships.json`
  - `media.json`
  - `intents.json`
  - `metadata.json`
- Added PostgreSQL serving export:
  - `city_entities`
  - `entity_intents`
  - `entity_sources`
  - `entity_reviews`
  - `entity_media`
  - `entity_relationships`
  - `crawler_source_pages`
- Changed open-web crawling so search result titles and list/article page titles are not treated as final app entities.
- Added source/list page storage under `data/city/source_pages` for audit and evidence.
- Added mentioned-entity extraction from article/list pages so real places, events, and communities become provisional entities.
- Added automatic OpenStreetMap follow-up candidates for extracted mentions to fill address and coordinates.
- Added Postgres cleanup/skip rules for obvious stale source-like article entities in `city_entities`.
- Added production-readiness validation with hard gates for real entity name, category, locality, address, latitude, longitude, source provenance, non-article title, and recommendation-eligible entity type.
- Added `validate-production` CLI command and `production_validation.json` audit report.
- Added recursive enrichment enqueueing for entities missing address, coordinates, or image candidates through OpenStreetMap plus Firecrawl/open-web search, and Google Custom Search when official credentials are configured.
- Added parallel crawl execution with a separate background enrichment thread pool. After each extracted entity is saved, production validation runs immediately and missing address/coordinate/image enrichment candidates are fed back into the active crawl frontier.
- Hardened runtime/data integrity before batch execution:
  - orchestrator and adapters now share the same `JsonStore` lock during parallel runs
  - Firecrawl is used for recursive mention search when Google official API credentials are absent
  - raw/source-page filenames include stable hashes to avoid silent overwrites
  - malformed frontier/entity JSON is skipped with warnings instead of stopping the full batch
  - Postgres app-serving rows are retained only after successful blob-image export
  - per-entity Postgres child rows are replaced on export to avoid stale media, intents, reviews, sources, and relationships
  - image blob downloads retry transient failures and respect 429 `Retry-After`
- Changed Postgres export so `city_entities` is rebuilt as a production-serving table containing only validation-passing rows.
- Added `crawler_entity_quality` Postgres audit table for blocked/non-ready entities and enrichment reasons.
- Added `crawler_entities_raw` Postgres staging table so every crawled entity persists for inspection even when it is not production-ready.
- Added DB blob image storage: `entity_media.image_blob`, `mime_type`, `byte_size`, `content_hash`, and `city_entities.primary_image_id`.
- Changed app-facing export to store image bytes in PostgreSQL and keep remote image URLs only as provenance in `entity_media.source_url`.
- Added `postgres-export` CLI command.
- Added detailed logging for:
  - CLI command startup
  - crawl batch start/end
  - candidate processing
  - source search
  - web page extraction
  - HTTP request/response/retry/robots status
  - dedupe merges
  - entity saves
  - Postgres export.

## Still Limited By Source Access

The crawler now has fields and extraction paths for the final standard, but some
sources cannot be fully harvested unless official access is configured:

- Google Maps reviews/photos require Google Places/Maps API and storage/display compliance.
- Reddit requires official API credentials.
- YouTube comments require YouTube Data API quota.
- Instagram/Facebook require Meta Graph API access.
- Meetup/Eventbrite and ticketing platforms require official APIs or approved feeds.
- Zomato/Swiggy/TripAdvisor/BookMyShow/District/Paytm Insider require official/partner access or manual imports.

The crawler does not bypass login walls, paywalls, robots.txt, or platform anti-bot controls.

## Production Readiness Checklist

- [x] Final-standard schema coverage.
- [x] Public page structured extraction.
- [x] Firecrawl local search/page integration.
- [x] OSM/Wikipedia/Wikidata integration.
- [x] Filesystem canonical/audit storage.
- [x] Postgres serving export.
- [x] Runtime status logging.
- [x] Source-page separation from app entities.
- [x] Mention-to-entity extraction with OSM geocoding follow-up.
- [x] Production serving quality gate.
- [x] Recursive enrichment queue for missing address/coordinates/images.
- [x] Simultaneous crawl plus background enrichment execution.
- [x] DB audit of non-ready entities.
- [x] Image blob storage for app-facing media.
- [x] Final runtime and data-integrity hardening pass.
- [ ] Source-specific official API adapters for gated platforms.
- [ ] LLM extractor integration for higher precision evidence maps.
- [ ] Image download and computer-vision classification.
- [ ] Scheduler/daemon deployment with metrics dashboard.
