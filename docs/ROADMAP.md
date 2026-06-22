# Roadmap

## Phase 1: Public Open Data

- OpenStreetMap category expansion across Hyderabad.
- Wikipedia/Wikidata entity expansion.
- Filesystem storage, provenance, checkpointing, coverage indexes.

## Phase 2: Text and Discussion Imports

- Enable Google Programmable Search credentials for broad web discovery.
- Add domain-specific `web_page` extraction rules for high-value Hyderabad tourism,
  news, government, event, college, NGO, and community sites.
- Add official Reddit API adapter for public Hyderabad subreddit discussions.
- Add YouTube Data API adapter for public video/comment discovery.
- Add configurable RSS/blog/news/government portal adapters after robots review.
- Extract venue/activity/community mentions from long text signals.

## Phase 3: Media and Graph Intelligence

- Download permitted images with license/provenance.
- Add image classifier labels.
- Build relationship inference: near, located_in, similar_to, hosts, organizes,
  popular_with, recommended_for.

## Phase 4: Commercial/Partner Sources

- Add official Google Places ingestion respecting API terms.
- Add partner/API importers for event and food platforms.
- Add manual CSV/JSON imports for licensed datasets.

## Phase 5: Continuous Coverage Optimization

- Plateau detection by locality/category/source.
- Priority planner for unexplored localities and missing categories.
- Freshness scheduler for stale entities, expired events, and new reviews.
