# CupidAI Crawler

Autonomous, resumable, filesystem-first city knowledge crawler for Hyderabad.
The crawler itself is the product: it discovers public entities, recursively
expands crawl candidates, deduplicates entities, enriches metadata, tracks
coverage, and writes a human-readable city knowledge base for future
recommendation and intent systems.

The initial city is Hyderabad, but the core is adapter-based so new sources and
cities can be added without changing the storage contract.

## Capabilities

- Discovers venues, businesses, attractions, communities, activities, events,
  landmarks, parks, lakes, museums, theaters, gyms, coworking spaces, NGOs, and
  other public city entities.
- Crawls public/open sources through source-specific adapters.
- Recursively expands discovery from entities, websites, links, and mentions.
- Stores raw source payloads for traceability.
- Writes canonical entity folders with JSON files and media directories.
- Deduplicates entities using name, address, geo, website, and content signals.
- Enriches entities with topics, sentiment, audience hints, popularity,
  hidden-gem score, and intent tags.
- Tracks coverage by source, category, locality, and frontier size.
- Runs in bounded batches so it can be scheduled continuously without losing
  progress.

## What It Builds

Generated data is stored under `data/`. The repository tracks the folder
structure, but ignores crawl output so large local datasets are not committed.

```text
data/
  city/
    entities/{id}/
      entity.json
      reviews.json
      comments.json
      intents.json
      relationships.json
      metadata.json
      images/
      videos/
    indexes/
      coverage.json
  checkpoints/
    frontier.json
  raw/
    {source}/...
```

Each entity can contain name, aliases, category, description, locality, address,
coordinates, timings, contact info, website, social links, ratings, reviews,
comments, media, amenities, pricing, audience, popularity, source provenance,
relationships, and generated intent metadata.

## Repository Layout

```text
collector/
  adapters/          Source adapters: OSM, Wikipedia, Wikidata, Google Search, web pages
  core/              Models, storage, orchestration, HTTP, dedupe, enrichment, coverage
  pipelines/         Reserved for deeper extraction/media pipelines
  utils/             Reserved for shared utility modules
configs/             Seed category and intent configuration
data/                Local crawl output root, ignored except .gitkeep files
docs/                Roadmap and operating notes
```

## Source Policy

Enabled by default:

- OpenStreetMap / Overpass API
- Wikipedia MediaWiki API
- Wikidata API
- Official Google Programmable Search JSON API, when credentials are configured
- Generic public web pages discovered through search results or manual seeds
- Local/self-hosted Firecrawl search and page scraping, when configured

Commercial and social platforms such as Google Maps, Instagram, Facebook,
TripAdvisor, Zomato, Swiggy, BookMyShow, District, Paytm Insider, Meetup,
Eventbrite, Reddit, and YouTube are represented in the source registry but are
disabled until official API credentials, partner access, or explicit permission
is configured. The collector does not bypass logins, paywalls, bot protections,
or robots.txt. It also does not scrape Google result pages; it uses the official
Google Custom Search JSON API.

## Requirements

- Python 3.11 or newer.
- Network access for public APIs and permitted web pages.
- Optional Google Programmable Search credentials for dynamic open-web search.

## Setup

```powershell
git clone https://github.com/CupidAiOfficial/crawler.git
cd crawler
python -m pip install -e .
Copy-Item .env.example .env
```

To enable dynamic Google web discovery, create a Google Programmable Search
Engine and set:

```env
GOOGLE_CUSTOM_SEARCH_API_KEY=...
GOOGLE_CUSTOM_SEARCH_ENGINE_ID=...
```

Without those values, OSM/Wikipedia/Wikidata and manually seeded `web_page` URLs
still work, but `google_search` candidates remain retryable with a clear
configuration error.

## Commands

Initialize folders and seed Hyderabad discovery:

```powershell
python -m collector.cli init
```

Run a bounded, resumable crawl batch:

```powershell
python -m collector.cli run --max-candidates 25
```

Add custom seeds:

```powershell
python -m collector.cli seed openstreetmap "badminton"
python -m collector.cli seed wikipedia "Hyderabad startup communities"
python -m collector.cli seed google_search "Hyderabad pottery workshops"
python -m collector.cli seed web_page "https://en.wikipedia.org/wiki/Hyderabad"
python -m collector.cli seed firecrawl_search "places to visit after midnight in hyderabad"
```

Inspect coverage:

```powershell
python -m collector.cli coverage
```

Inspect source policy:

```powershell
python -m collector.cli sources
```

## Typical Operating Loop

```powershell
python -m collector.cli init
python -m collector.cli run --max-candidates 50
python -m collector.cli coverage
```

Schedule the `run` command repeatedly. Each run loads `frontier.json`, crawls a
bounded batch, writes entities/raw data, refreshes coverage, and saves the
remaining frontier.

## Dynamic Web Discovery

Dynamic Google search uses the official Custom Search JSON API:

```powershell
python -m collector.cli seed google_search "Hyderabad indie music events"
python -m collector.cli run --max-candidates 25
```

Manual public page seeds work without Google credentials:

```powershell
python -m collector.cli seed web_page "https://en.wikipedia.org/wiki/Hyderabad"
python -m collector.cli run --max-candidates 5
```

The web page adapter respects the configured robots policy, parses HTML
metadata, JSON-LD, text, images, and links, creates a canonical entity when
relevant, and emits new crawl candidates for Hyderabad-relevant links and
mentions.

## Architecture

- `collector/core/models.py` defines stable JSON contracts.
- `collector/core/storage.py` writes filesystem-first entity folders.
- `collector/core/orchestrator.py` manages frontier checkpoints and recursive crawling.
- `collector/core/dedupe.py` resolves duplicates using name, geo, address, and website signals.
- `collector/core/enrichment.py` generates topics, sentiment, audience, hidden-gem, popularity, and intent tags.
- `collector/adapters/google_search.py` discovers public web pages through the official Google API.
- `collector/adapters/web_page.py` crawls allowed pages, parses metadata/JSON-LD/text/links/images, and emits more candidates.
- `collector/adapters/firecrawl.py` uses a local/self-hosted Firecrawl API for search and page extraction.
- `collector/adapters/source_registry.py` documents enabled and gated sources.

## Configuration

The following environment variables can be set in `.env`:

```env
CITY_NAME=Hyderabad
DATA_ROOT=data
USER_AGENT=HyderabadCityKnowledgeCollector/0.1 contact=you@example.com
MAX_DEPTH=3
MAX_CANDIDATES_PER_RUN=50
REQUEST_DELAY_SECONDS=1.2
RESPECT_ROBOTS=true
GOOGLE_CUSTOM_SEARCH_API_KEY=
GOOGLE_CUSTOM_SEARCH_ENGINE_ID=
WEB_SEARCH_RESULTS_PER_QUERY=10
WEB_PAGE_MAX_LINKS=20
WEB_PAGE_MAX_CHARS=12000
FIRECRAWL_BASE_URL=http://localhost:3002
FIRECRAWL_API_KEY=
FIRECRAWL_SEARCH_LIMIT=10
FIRECRAWL_SCRAPE_FORMATS=["markdown","html"]
```

## Local Firecrawl

To use Firecrawl on your laptop:

```powershell
.\scripts\setup_firecrawl_local.ps1 -Start
python -m collector.cli seed firecrawl_search "places to visit after midnight in hyderabad"
python -m collector.cli run --max-candidates 20
```

Or use the combined runner, which starts Firecrawl, waits for it, seeds the
query, runs a crawler batch, and prints coverage:

```powershell
.\scripts\run_firecrawl_crawler.ps1 -Query "places to visit after midnight in hyderabad" -MaxCandidates 20
```

To stop Firecrawl automatically after the batch:

```powershell
.\scripts\run_firecrawl_crawler.ps1 -Query "late night food places in hyderabad" -MaxCandidates 30 -StopAfterRun
```

See [docs/FIRECRAWL_LOCAL.md](docs/FIRECRAWL_LOCAL.md) for details.

## Operating Model

The crawler is designed to run repeatedly:

1. Load `data/checkpoints/frontier.json`.
2. Crawl a bounded number of candidates.
3. Store raw source payloads.
4. Normalize entities.
5. Merge duplicates into canonical entities.
6. Generate metadata and coverage.
7. Add newly discovered candidates.
8. Save progress before exiting.

Use Task Scheduler, cron, or a service wrapper to run batches continuously.

## Data and Git Hygiene

The generated city memory can become large. The repository ignores:

- raw crawl payloads
- generated entity folders
- generated coverage indexes
- generated frontier checkpoints
- `.env`
- Python cache directories

Use object storage, a database export, or a separate data repository if you need
to share collected datasets.

## Compliance Notes

This crawler is built for public/open data and permitted crawling. It should not
be used to bypass authentication, paywalls, bot protection, platform terms,
robots.txt, or API storage restrictions. For commercial/social platforms, add
official API or partner adapters instead of scraping blocked pages.
