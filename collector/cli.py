from __future__ import annotations

import argparse
import json
from pathlib import Path

from collector.adapters.source_registry import SOURCE_REGISTRY
from collector.core.config import settings
from collector.core.coverage import CoverageTracker
from collector.core.models import CandidateKind, CrawlCandidate
from collector.core.storage import JsonStore
from collector.factory import build_orchestrator


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hyd-collector",
        description="Autonomous filesystem-first Hyderabad city knowledge collector",
    )
    parser.add_argument("--data-root", default=str(settings.data_root), help="Filesystem data root")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Create data folders and seed the Hyderabad crawl frontier")

    run = sub.add_parser("run", help="Run one resumable crawl batch")
    run.add_argument("--max-candidates", type=int, default=settings.max_candidates_per_run)

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
    sub.add_parser("sources", help="Print source policy registry")

    args = parser.parse_args()
    data_root = Path(args.data_root)

    if args.command == "init":
        orchestrator = build_orchestrator(data_root)
        orchestrator.bootstrap_hyderabad()
        print(f"Initialized {data_root.resolve()} and seeded Hyderabad crawl frontier.")
        return

    if args.command == "run":
        orchestrator = build_orchestrator(data_root)
        orchestrator.run(max_candidates=args.max_candidates)
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

    if args.command == "sources":
        print(json.dumps(SOURCE_REGISTRY, indent=2, default=str))
        return


if __name__ == "__main__":
    main()
