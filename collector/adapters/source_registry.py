from __future__ import annotations

from collector.core.models import SourcePolicy


SOURCE_REGISTRY: dict[str, dict[str, object]] = {
    "openstreetmap": {
        "policy": SourcePolicy.OPEN_DATA,
        "enabled": True,
        "notes": "Use Overpass API politely; preserve ODbL attribution.",
    },
    "wikidata": {
        "policy": SourcePolicy.OPEN_DATA,
        "enabled": True,
        "notes": "Use Wikidata API; preserve CC0/source metadata.",
    },
    "wikipedia": {
        "policy": SourcePolicy.PUBLIC_API,
        "enabled": True,
        "notes": "Use MediaWiki API; preserve CC BY-SA attribution.",
    },
    "google_search": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": True,
        "notes": "Uses official Google Programmable Search JSON API when GOOGLE_CUSTOM_SEARCH_API_KEY and GOOGLE_CUSTOM_SEARCH_ENGINE_ID are configured. Does not scrape Google result pages.",
    },
    "web_page": {
        "policy": SourcePolicy.HTML_ALLOWED,
        "enabled": True,
        "notes": "Crawls result pages and seed URLs that allow crawling; respects robots.txt and stores extracted page metadata/provenance. JS-heavy public pages can fall back to Firecrawl rendering, but access controls are not bypassed.",
    },
    "firecrawl_search": {
        "policy": SourcePolicy.HTML_ALLOWED,
        "enabled": True,
        "notes": "Uses a configured local/self-hosted Firecrawl API to search the web and enqueue result pages. Prefer SearXNG-backed search for fully local operation.",
    },
    "firecrawl_page": {
        "policy": SourcePolicy.HTML_ALLOWED,
        "enabled": True,
        "notes": "Uses a configured local/self-hosted Firecrawl API to render public pages into markdown/html before entity extraction. Do not use it to bypass paywalls, login walls, robots exclusions, or anti-bot restrictions.",
    },
    "reddit": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official Reddit API credentials; store public posts/comments only.",
    },
    "youtube": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use YouTube Data API for videos/comments; obey quota and terms.",
    },
    "facebook": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use Meta Graph API for permitted public pages/events only.",
    },
    "instagram": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use Instagram Graph API for authorized public business/creator data.",
    },
    "google_maps": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use Google Places APIs and comply with storage/display restrictions.",
    },
    "tripadvisor": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use approved partner/API access or manual imports.",
    },
    "zomato": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official/partner access; do not scrape blocked pages.",
    },
    "swiggy": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official/partner access; do not scrape blocked pages.",
    },
    "bookmyshow": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official feed/API/partner access for events.",
    },
    "district": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official feed/API/partner access for events.",
    },
    "paytm_insider": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use official feed/API/partner access for events.",
    },
    "meetup": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use Meetup API/OAuth where available.",
    },
    "eventbrite": {
        "policy": SourcePolicy.OFFICIAL_API_REQUIRED,
        "enabled": False,
        "notes": "Use Eventbrite API token for public events.",
    },
    "news_blogs_tourism_government": {
        "policy": SourcePolicy.HTML_ALLOWED,
        "enabled": False,
        "notes": "Enable per-domain only after robots.txt and copyright policy review.",
    },
}
