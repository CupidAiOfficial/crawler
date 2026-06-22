from collector.adapters.firecrawl import FirecrawlPageAdapter, FirecrawlSearchAdapter
from collector.adapters.google_search import GoogleSearchAdapter
from collector.adapters.osm import OpenStreetMapAdapter
from collector.adapters.web_page import WebPageAdapter
from collector.adapters.wikidata import WikidataAdapter
from collector.adapters.wikipedia import WikipediaAdapter

__all__ = [
    "GoogleSearchAdapter",
    "FirecrawlPageAdapter",
    "FirecrawlSearchAdapter",
    "OpenStreetMapAdapter",
    "WebPageAdapter",
    "WikidataAdapter",
    "WikipediaAdapter",
]
