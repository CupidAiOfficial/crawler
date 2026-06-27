from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from collector.adapters.source_registry import SOURCE_REGISTRY
from collector.core.config import settings
from collector.core.coverage import CoverageTracker
from collector.core.image_enrichment import AppImageEnricher
from collector.core.mobile_export import MobileCardExporter
from collector.core.models import CandidateKind, CrawlCandidate
from collector.core.postgres_export import PostgresServingWriter
from collector.core.quality import ProductionReadinessValidator
from collector.core.refine import RefinementPipeline
from collector.core.storage import JsonStore
from collector.core.structured_bulk import OSM_QUERIES, StructuredBulkCollector
from collector.factory import build_orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hyd-collector",
        description="Autonomous filesystem-first Hyderabad city knowledge collector",
    )
    parser.add_argument("--data-root", default=str(settings.data_root), help="Filesystem data root")
    parser.add_argument("--log-level", default=settings.log_level, help="Python log level, e.g. INFO or DEBUG")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create data folders and seed the Hyderabad crawl frontier")

    run = sub.add_parser("run", help="Run one resumable crawl batch")
    run.add_argument("--max-candidates", type=int, default=settings.max_candidates_per_run)
    run.add_argument("--workers", type=int, default=settings.crawler_workers, help="Parallel crawl workers for I/O-bound candidates")

    seed = sub.add_parser("seed", help="Add a custom query candidate")
    seed.add_argument(
        "source",
        choices=[
            "openstreetmap",
            "wikipedia",
            "wikidata",
            "google_search",
            "web_page",
            "firecrawl_search",
            "firecrawl_page",
        ],
    )
    seed.add_argument("value")
    seed.add_argument("--priority", type=float, default=1.0)

    sub.add_parser("coverage", help="Write and print coverage snapshot")
    bulk = sub.add_parser("structured-bulk", help="High-yield structured acquisition from OSM/Wikidata/Wikipedia")
    bulk.add_argument(
        "--categories",
        default=None,
        help=f"Comma-separated category groups. Defaults to all: {','.join(OSM_QUERIES)}",
    )
    bulk.add_argument("--limit", type=int, default=None, help="Maximum entities to save in this run")
    bulk.add_argument("--max-wikimedia", type=int, default=None, help="Maximum entities to enrich through Wikidata/Wikipedia")
    bulk.add_argument("--skip-wikimedia", action="store_true", help="Skip Wikidata/Wikipedia/Commons enrichment")
    bulk.add_argument("--skip-refine", action="store_true", help="Skip refinement after structured collection")
    refine = sub.add_parser("refine", help="Recompute enrichment, app-card fields, and evidence after crawling")
    refine.add_argument(
        "--production-web-enrich",
        action="store_true",
        help="Search/scrape public result pages to fill missing production fields for plausible entities",
    )
    refine.add_argument(
        "--max-web-enrich",
        type=int,
        default=200,
        help="Maximum entities to web-enrich in this refine pass",
    )
    refine.add_argument("--max-entities", type=int, default=None, help="Maximum entities to refine in this pass")
    refine.add_argument("--skip-open-image", action="store_true", help="Skip opportunistic Wikipedia image lookups during refine")
    validate = sub.add_parser("validate-production", help="Validate entities for production app serving")
    validate.add_argument("--enqueue", action="store_true", help="Seed enrichment searches for entities missing required data")
    validate.add_argument("--max-entities", type=int, default=200, help="Maximum non-ready entities to enqueue enrichment for")
    image_enrich = sub.add_parser("image-enrich", help="Add app-serving images to otherwise valid entities")
    image_enrich.add_argument("--target-ready", type=int, default=500, help="Production-ready target count")
    image_enrich.add_argument("--limit", type=int, default=None, help="Maximum images to add in this pass")
    image_enrich.add_argument(
        "--allow-generated-location-cards",
        action="store_true",
        help="Generate truthful entity-specific location card PNGs when no open/source photo is available",
    )
    sub.add_parser("normalize-app-branches", help="Normalize entities into Explore, Date, Build, Network branches")
    sub.add_parser("sources", help="Print source policy registry")
    mobile = sub.add_parser("mobile-index", help="Write app-ready mobile search cards")
    mobile.add_argument("--query", default=None, help="Optional query to rank cards, e.g. 'quiet places to walk near Begumpet'")
    mobile.add_argument("--limit", type=int, default=100)
    mobile.add_argument("--lat", type=float, default=None, help="Optional user latitude for distance ranking")
    mobile.add_argument("--lon", type=float, default=None, help="Optional user longitude for distance ranking")
    mobile.add_argument("--output", default="mobile_cards.json", help="Index filename under data/city/indexes")
    pg = sub.add_parser("postgres-export", help="Export canonical crawler data to PostgreSQL serving tables")
    pg.add_argument("--database-url", default=settings.database_url, help="PostgreSQL URL; defaults to DATABASE_URL")

    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    data_root = Path(args.data_root)

    if args.command == "init":
        orchestrator = build_orchestrator(data_root)
        orchestrator.bootstrap_hyderabad()
        print(f"Initialized {data_root.resolve()} and seeded Hyderabad crawl frontier.")
        return

    if args.command == "run":
        orchestrator = build_orchestrator(data_root)
        orchestrator.run(max_candidates=args.max_candidates, workers=max(1, args.workers))
        print(f"Completed crawl batch. Coverage written to {data_root / 'city' / 'indexes' / 'coverage.json'}.")
        return

    if args.command == "seed":
        orchestrator = build_orchestrator(data_root)
        orchestrator.seed(
            [
                CrawlCandidate(
                    kind=CandidateKind.QUERY,
                    source=args.source,
                    value=args.value,
                    priority=args.priority,
                )
            ]
        )
        print(f"Seeded {args.source}:{args.value}")
        return

    if args.command == "coverage":
        store = JsonStore(data_root)
        snapshot = CoverageTracker(store).snapshot(store.load_frontier())
        print(json.dumps(snapshot.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return

    if args.command == "structured-bulk":
        store = JsonStore(data_root)
        categories = [item.strip() for item in args.categories.split(",") if item.strip()] if args.categories else None
        report = StructuredBulkCollector(store).run(
            categories=categories,
            limit=args.limit,
            enrich_wikimedia=not args.skip_wikimedia,
            max_wikimedia=args.max_wikimedia,
            refine_after=not args.skip_refine,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    if args.command == "refine":
        store = JsonStore(data_root)
        count = RefinementPipeline(
            store,
            production_web_enrich=args.production_web_enrich,
            max_web_enrich=args.max_web_enrich,
            max_entities=args.max_entities,
            fetch_open_images=not args.skip_open_image,
        ).run()
        print(f"Refined {count} canonical entities.")
        return

    if args.command == "validate-production":
        store = JsonStore(data_root)
        validator = ProductionReadinessValidator()
        results = validator.validate_store(store)
        enqueued = validator.enqueue_enrichment(store, max_entities=args.max_entities) if args.enqueue else 0
        ready = sum(1 for result in results if result.production_ready)
        blocked = len(results) - ready
        print(json.dumps({
            "total": len(results),
            "production_ready": ready,
            "blocked": blocked,
            "enrichment_candidates_enqueued": enqueued,
            "report": str(store.indexes_root / "production_validation.json"),
        }, indent=2))
        return

    if args.command == "image-enrich":
        store = JsonStore(data_root)
        report = AppImageEnricher(store).run(
            target_ready=args.target_ready,
            limit=args.limit,
            allow_generated_cards=args.allow_generated_location_cards,
        )
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    if args.command == "normalize-app-branches":
        store = JsonStore(data_root)
        report = AppImageEnricher(store).normalize_store_branches()
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    if args.command == "sources":
        print(json.dumps(SOURCE_REGISTRY, indent=2, default=str))
        return

    if args.command == "mobile-index":
        store = JsonStore(data_root)
        path = MobileCardExporter(store).write_index(
            query=args.query,
            limit=args.limit,
            user_latitude=args.lat,
            user_longitude=args.lon,
            filename=args.output,
        )
        print(f"Wrote mobile card index to {path}")
        print(path.read_text(encoding="utf-8"))
        return

    if args.command == "postgres-export":
        if not args.database_url:
            raise SystemExit("DATABASE_URL is required. Pass --database-url or set DATABASE_URL in .env.")
        store = JsonStore(data_root)
        count = PostgresServingWriter(args.database_url).export_store(store)
        print(f"Exported {count} canonical entities to PostgreSQL.")
        return


if __name__ == "__main__":
    main()
