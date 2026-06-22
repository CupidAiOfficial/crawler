# Local Firecrawl Setup

This crawler can use a local/self-hosted Firecrawl instance as its webpage
search and extraction backend.

Firecrawl is useful when normal HTML extraction is too weak, especially for
JavaScript-heavy pages. The CupidAI crawler still owns the city-memory layer:
frontier, dedupe, entity JSON, metadata, relationships, and coverage.

## Requirements

- Docker Desktop running
- Git
- Recommended: 16 GB RAM or more
- Free local disk space for Docker images

## Install Firecrawl Locally

From this repository:

```powershell
.\scripts\setup_firecrawl_local.ps1
```

This clones Firecrawl into:

```text
V:\CupidAi\firecrawl
```

and creates a local `.env`.

To build and start it immediately:

```powershell
.\scripts\setup_firecrawl_local.ps1 -Start
```

Firecrawl should then be available at:

```text
http://localhost:3002
```

Queue UI:

```text
http://localhost:3002/admin/local-firecrawl-admin/queues
```

The official self-hosting guide says local instances can be accessed at
`http://localhost:3002` after `docker compose build` and `docker compose up`.
Self-hosted instances do not have access to Fire-engine, so advanced anti-block
handling may require your own proxy/browser configuration.

## Configure This Crawler

In `.env`:

```env
FIRECRAWL_BASE_URL=http://localhost:3002
FIRECRAWL_API_KEY=
FIRECRAWL_SEARCH_LIMIT=10
FIRECRAWL_SCRAPE_FORMATS=["markdown","html"]
```

Self-hosted Firecrawl does not require an API key by default when
`USE_DB_AUTHENTICATION=false`.

## Run A Search Term

Example:

```powershell
python -m collector.cli seed firecrawl_search "places to visit after midnight in hyderabad"
python -m collector.cli run --max-candidates 20
python -m collector.cli coverage
```

Flow:

1. `firecrawl_search` sends the query to local Firecrawl.
2. Firecrawl returns result URLs and page context.
3. The crawler enqueues those URLs as `firecrawl_page`.
4. `firecrawl_page` scrapes each page into Markdown/HTML.
5. The crawler extracts Hyderabad-relevant entity records.
6. Entity JSON is written under `data/city/entities/{id}/`.

## Search Engine Notes

Firecrawl self-host docs say `/search` uses Google search by default and can be
configured to use SearXNG instead. For a more local/open setup, run SearXNG and
set Firecrawl's `SEARXNG_ENDPOINT`.

Do not use this setup to bypass login walls, paywalls, platform terms, robots
policies, or bot protections. Use official APIs or licensed datasets for
restricted platforms.

## Useful Checks

```powershell
docker compose ps
Invoke-RestMethod http://localhost:3002/v2/search -Method Post -ContentType "application/json" -Body '{"query":"Hyderabad cafes","limit":3}'
```

If your local Firecrawl build exposes only v1 endpoints, update
`FIRECRAWL_BASE_URL` normally; the crawler adapter uses v2 endpoints by default.
