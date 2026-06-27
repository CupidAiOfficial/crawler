# Crawler Access Policy

The crawler may collect public pages, public open-data APIs, and official API
responses where the source permits access and reuse.

Allowed:

- Open data APIs such as OpenStreetMap, Wikidata, and Wikipedia.
- Official APIs with configured credentials and compliant storage/display use.
- Public HTML pages that allow crawling.
- Browser-rendered extraction for public JavaScript-heavy pages when the content
  is available without login, paywall, or anti-bot circumvention.

Not allowed:

- Bypassing robots.txt exclusions.
- Bypassing login walls, paywalls, rate limits, CAPTCHAs, or anti-bot systems.
- Reusing private/session-only browser cookies to access restricted content.
- Scraping platform pages where official/partner API access is required.

Production serving rules:

- `city_entities` is an app-serving table, not a raw crawl dump.
- Rows must pass production validation before export.
- Required app-serving fields include real entity name, category, locality,
  address, latitude, longitude, source provenance, recommendation-eligible type,
  and DB-stored image blob.
- Raw candidates, source pages, failures, and non-ready entities remain in audit
  tables/files until enrichment makes them production-ready.
