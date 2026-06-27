from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from collector.core.config import settings
from collector.core.models import CityEntity, MediaAsset, Rating, SourceRecord
from collector.core.quality import ProductionReadinessValidator


logger = logging.getLogger(__name__)


LOCALITY_HINTS = [
    "banjara hills",
    "jubilee hills",
    "madhapur",
    "hitec city",
    "gachibowli",
    "kondapur",
    "begumpet",
    "ameerpet",
    "secunderabad",
    "kukatpally",
    "charminar",
    "abids",
    "financial district",
    "nampally",
    "tank bund",
]

FOOD_CUISINE_TERMS = [
    "north eastern",
    "asian",
    "korean",
    "thai",
    "momos",
    "biryani",
    "cafe",
    "coffee",
    "bakery",
    "restaurant",
]


@dataclass
class WebEvidence:
    source: str
    url: str
    title: str | None = None
    text: str = ""
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    phone: str | None = None
    opening_hours: str | None = None
    price: str | None = None
    rating: float | None = None
    rating_count: int | None = None
    image_urls: list[str] = field(default_factory=list)
    amenities: list[str] = field(default_factory=list)
    cuisines: list[str] = field(default_factory=list)
    confidence: float = 0.0


class ProductionWebEnricher:
    """Searches public pages for missing production-card fields.

    The goal is conservative promotion: enrich only plausible real entities, and
    merge fields only when a source page names the entity and is Hyderabad-local.
    """

    def __init__(self) -> None:
        self.validator = ProductionReadinessValidator()
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._last_nominatim_at = 0.0

    def enrich_entity(self, entity: CityEntity) -> CityEntity:
        quality = self.validator.validate(entity)
        if quality.production_ready or not self.validator.should_enqueue_enrichment(entity, quality):
            return entity
        if self._generic_search_entity(entity):
            return entity
        if not self._needs_web_enrichment(entity):
            return entity
        logger.info(
            "production web enrichment start entity_id=%s name=%s missing=%s blockers=%s",
            entity.id,
            entity.name,
            quality.missing_fields,
            quality.blockers,
        )
        evidence_items = self._collect_evidence(entity)
        if not evidence_items:
            logger.info("production web enrichment no evidence entity_id=%s name=%s", entity.id, entity.name)
            return entity
        entity = self._merge_evidence(entity, evidence_items)
        logger.info(
            "production web enrichment complete entity_id=%s name=%s evidence=%s address=%s geo=%s,%s media=%s",
            entity.id,
            entity.name,
            len(evidence_items),
            bool(entity.address),
            entity.latitude,
            entity.longitude,
            len(entity.media),
        )
        return entity

    def _needs_web_enrichment(self, entity: CityEntity) -> bool:
        return (
            not entity.address
            or entity.latitude is None
            or entity.longitude is None
            or not entity.locality
            or not entity.media
            or not entity.card.primary_image_url
        )

    def _collect_evidence(self, entity: CityEntity) -> list[WebEvidence]:
        results = self._search(entity)
        evidence: list[WebEvidence] = []
        seen_urls: set[str] = set()
        for result in results[:8]:
            url = result.get("url") or result.get("link")
            title = result.get("title")
            snippet = result.get("description") or result.get("snippet") or result.get("markdown") or ""
            if not isinstance(url, str) or not url.startswith(("http://", "https://")):
                continue
            if url in seen_urls or self._blocked_url(url):
                continue
            seen_urls.add(url)
            seed = WebEvidence(
                source=self._source_name(url),
                url=url,
                title=str(title) if title else None,
                text=str(snippet or ""),
            )
            self._extract_from_text(seed, entity, seed.text)
            if self._evidence_matches(seed, entity):
                evidence.append(seed)
            if len(evidence) >= 4:
                break
            page = self._scrape_page(url)
            if page and self._evidence_matches(page, entity):
                evidence.append(page)
            if len(evidence) >= 4:
                break
        return sorted(evidence, key=lambda item: item.confidence, reverse=True)

    def _search(self, entity: CityEntity) -> list[dict[str, object]]:
        queries = self._queries(entity)
        all_items: list[dict[str, object]] = []
        seen: set[str] = set()
        for query in queries:
            items = self._firecrawl_search(query)
            if not items and settings.google_custom_search_api_key and settings.google_custom_search_engine_id:
                items = self._google_custom_search(query)
            for item in items:
                url = item.get("url") or item.get("link")
                key = str(url or item.get("title") or "").lower()
                if key and key not in seen:
                    all_items.append(item)
                    seen.add(key)
            if len(all_items) >= 12:
                break
        logger.info("production web search entity=%s results=%s", entity.name, len(all_items))
        return all_items

    def _queries(self, entity: CityEntity) -> list[str]:
        base = self._search_name(entity.name)
        locality = entity.locality if entity.locality and entity.locality.lower() != "hyderabad" else ""
        parts = [base, locality, "Hyderabad"]
        core = " ".join(part for part in parts if part)
        queries = [
            f"{core} address latitude longitude",
            f"{core} Zomato",
            f"{core} Google Maps",
            f"{core} photos",
        ]
        if entity.primary_category == "restaurant" or entity.category == "restaurant":
            queries.insert(1, f"{core} restaurant address menu")
        return list(dict.fromkeys(query.strip() for query in queries if query.strip()))

    def _firecrawl_search(self, query: str) -> list[dict[str, object]]:
        try:
            logger.info("production firecrawl search query=%s", query)
            response = self._client.post(
                f"{settings.firecrawl_base_url.rstrip('/')}/v2/search",
                json={"query": query, "limit": min(max(settings.firecrawl_search_limit, 1), 10)},
                headers=self._firecrawl_headers(),
            )
            if response.status_code in {404, 405}:
                return []
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.info("production firecrawl search skipped query=%s error=%s", query, exc)
            return []
        return self._items(payload)

    def _google_custom_search(self, query: str) -> list[dict[str, object]]:
        try:
            logger.info("production google custom search query=%s", query)
            response = self._client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={
                    "key": settings.google_custom_search_api_key,
                    "cx": settings.google_custom_search_engine_id,
                    "q": query,
                    "num": min(max(settings.web_search_results_per_query, 1), 10),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.info("production google custom search skipped query=%s error=%s", query, exc)
            return []
        items = payload.get("items") if isinstance(payload, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _scrape_page(self, url: str) -> WebEvidence | None:
        page = self._firecrawl_scrape(url)
        if page:
            return page
        return self._direct_scrape(url)

    def _firecrawl_scrape(self, url: str) -> WebEvidence | None:
        try:
            logger.info("production firecrawl scrape url=%s", url)
            response = self._client.post(
                f"{settings.firecrawl_base_url.rstrip('/')}/v2/scrape",
                json={"url": url, "formats": ["markdown", "html"], "onlyMainContent": True},
                headers=self._firecrawl_headers(),
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("production firecrawl scrape failed url=%s error=%s", url, exc)
            return None
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            return None
        html = str(data.get("html") or "")
        markdown = str(data.get("markdown") or "")
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        title = str(metadata.get("title") or "") if isinstance(metadata, dict) else None
        return self._evidence_from_page(url, title, markdown, html)

    def _direct_scrape(self, url: str) -> WebEvidence | None:
        try:
            logger.info("production direct scrape url=%s", url)
            response = self._client.get(url)
            response.raise_for_status()
        except Exception as exc:
            logger.debug("production direct scrape failed url=%s error=%s", url, exc)
            return None
        content_type = response.headers.get("content-type") or ""
        if "html" not in content_type and "<html" not in response.text[:500].lower():
            return None
        return self._evidence_from_page(str(response.url), None, "", response.text)

    def _evidence_from_page(self, url: str, title: str | None, markdown: str, html: str) -> WebEvidence:
        soup = BeautifulSoup(html, "html.parser") if html else None
        if not title and soup and soup.title and soup.title.string:
            title = self._clean(soup.title.string)
        text = markdown or self._clean(soup.get_text(" ") if soup else "")
        evidence = WebEvidence(source=self._source_name(url), url=url, title=title, text=text[:15000])
        self._extract_json_ld(evidence, soup)
        self._extract_meta_images(evidence, soup, url)
        self._extract_google_maps_links(evidence, soup, url)
        self._extract_from_text(evidence, None, " ".join([title or "", evidence.text]))
        return evidence

    def _extract_json_ld(self, evidence: WebEvidence, soup: BeautifulSoup | None) -> None:
        if not soup:
            return
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            for item in self._flatten_json(payload):
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                if isinstance(name, str) and not evidence.title:
                    evidence.title = name
                address = self._address_text(item.get("address"))
                if address:
                    evidence.address = evidence.address or address
                geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
                lat = self._float(geo.get("latitude")) if isinstance(geo, dict) else None
                lon = self._float(geo.get("longitude")) if isinstance(geo, dict) else None
                if lat is not None and lon is not None:
                    evidence.latitude = evidence.latitude or lat
                    evidence.longitude = evidence.longitude or lon
                telephone = item.get("telephone")
                if isinstance(telephone, str):
                    evidence.phone = evidence.phone or telephone
                opening = item.get("openingHours") or item.get("openingHoursSpecification")
                if opening:
                    evidence.opening_hours = evidence.opening_hours or self._opening_text(opening)
                image = item.get("image")
                evidence.image_urls.extend(self._image_values(image))
                rating = item.get("aggregateRating") if isinstance(item.get("aggregateRating"), dict) else {}
                if isinstance(rating, dict):
                    evidence.rating = evidence.rating or self._float(rating.get("ratingValue"))
                    evidence.rating_count = evidence.rating_count or self._int(rating.get("reviewCount") or rating.get("ratingCount"))
                price = item.get("priceRange")
                if isinstance(price, str):
                    evidence.price = evidence.price or price
                cuisine = item.get("servesCuisine")
                if isinstance(cuisine, str):
                    evidence.cuisines.extend([cuisine])
                elif isinstance(cuisine, list):
                    evidence.cuisines.extend(str(item) for item in cuisine)

    def _extract_meta_images(self, evidence: WebEvidence, soup: BeautifulSoup | None, url: str) -> None:
        if not soup:
            return
        for key in ["og:image", "twitter:image", "image"]:
            tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
            content = tag.get("content") if tag else None
            if isinstance(content, str) and content:
                evidence.image_urls.append(urljoin(url, content))
        for image in soup.find_all("img", src=True)[:30]:
            src = urljoin(url, image["src"])
            alt = self._clean(image.get("alt"))
            if src.startswith(("http://", "https://")) and (alt or "image" not in src.lower()):
                evidence.image_urls.append(src)

    def _extract_google_maps_links(self, evidence: WebEvidence, soup: BeautifulSoup | None, base_url: str) -> None:
        links: list[str] = [base_url]
        if soup:
            for anchor in soup.find_all("a", href=True):
                href = urljoin(base_url, anchor["href"])
                if "google.com/maps" in href or "maps.google" in href or "g.co/kgs" in href:
                    links.append(href)
        for link in links:
            coords = self._coords_from_url(link)
            if coords:
                evidence.latitude = evidence.latitude or coords[0]
                evidence.longitude = evidence.longitude or coords[1]

    def _extract_from_text(self, evidence: WebEvidence, entity: CityEntity | None, text: str) -> None:
        clean = self._clean(text)
        if not evidence.address:
            evidence.address = self._address_from_text(clean)
        coords = self._coords_from_text(clean)
        if coords and evidence.latitude is None and evidence.longitude is None:
            evidence.latitude, evidence.longitude = coords
        phone = re.search(r"(?:\+91[-\s]?)?[6-9]\d{9}", clean)
        if phone:
            evidence.phone = evidence.phone or phone.group(0)
        rating = re.search(r"\b([1-5](?:\.\d)?)\s*(?:/|out of)?\s*5\b", clean.lower())
        if rating:
            evidence.rating = evidence.rating or self._float(rating.group(1))
        count = re.search(r"\b([0-9][0-9,]{1,8})\s+(?:reviews|ratings)\b", clean.lower())
        if count:
            evidence.rating_count = evidence.rating_count or self._int(count.group(1))
        hours = re.search(r"((?:daily|mon|tue|wed|thu|fri|sat|sun|open)[^.\n]{0,120}(?:am|pm|hours))", clean.lower())
        if hours:
            evidence.opening_hours = evidence.opening_hours or hours.group(1)
        if not evidence.price:
            price = re.search(r"(?:₹|rs\.?\s*)([0-9][0-9,]{1,6})(?:\s*for\s*two)?", clean.lower())
            if price:
                evidence.price = f"₹{price.group(1)}"
        lower = clean.lower()
        evidence.amenities.extend(term for term in ["parking", "valet", "outdoor seating", "wifi", "kid friendly", "family"] if term in lower)
        evidence.cuisines.extend(term for term in FOOD_CUISINE_TERMS if term in lower)

    def _merge_evidence(self, entity: CityEntity, evidence_items: list[WebEvidence]) -> CityEntity:
        best = evidence_items[0]
        if best.address and self._valid_address(best.address):
            entity.address = entity.address or best.address
        if entity.latitude is None or entity.longitude is None:
            if best.latitude is not None and best.longitude is not None:
                entity.latitude = best.latitude
                entity.longitude = best.longitude
                entity.geo_precision = "source_page_coordinates"
            elif entity.address:
                self._geocode_verified_address(entity)
        entity.locality = entity.locality if entity.locality and entity.locality.lower() != "hyderabad" else self._locality_from_address(entity.address or best.text)
        entity.locality = entity.locality or "Hyderabad"
        if best.phone:
            phones = entity.contact.get("phone_numbers") or []
            if isinstance(phones, str):
                phones = [phones]
            entity.contact["phone_numbers"] = sorted(set(phones + [best.phone]))
        entity.opening_hours_raw = entity.opening_hours_raw or best.opening_hours
        if best.price:
            entity.price_level = entity.price_level or best.price
            entity.pricing.setdefault("observed_price_text", best.price)
        if best.rating is not None:
            entity.rating = entity.rating or best.rating
            if best.rating_count:
                entity.rating_count = entity.rating_count or best.rating_count
                entity.review_count = entity.review_count or best.rating_count
            if not any(rating.source == best.source for rating in entity.ratings):
                entity.ratings.append(Rating(source=best.source, score=best.rating, count=best.rating_count, scale=5))
        entity.amenities = sorted(set(entity.amenities + best.amenities))
        if best.cuisines:
            entity.subcategories = sorted(set(entity.subcategories + [self._slug(item) for item in best.cuisines]))
            entity.metadata.intent_tags = sorted(set(entity.metadata.intent_tags + [self._slug(item) for item in best.cuisines]))
        self._merge_images(entity, evidence_items)
        for item in evidence_items:
            entity.sources.append(
                SourceRecord(
                    source=item.source,
                    url=item.url,
                    source_type="production_web_enrichment",
                    source_name=urlparse(item.url).netloc,
                    extraction_confidence=item.confidence,
                    metadata={
                        "title": item.title,
                        "fields": self._fields(item),
                    },
                )
            )
        entity.raw_json["production_web_enrichment"] = {
            "evidence_count": len(evidence_items),
            "sources": [
                {
                    "source": item.source,
                    "url": item.url,
                    "title": item.title,
                    "confidence": round(item.confidence, 3),
                    "fields": self._fields(item),
                }
                for item in evidence_items
            ],
        }
        return entity

    def _merge_images(self, entity: CityEntity, evidence_items: list[WebEvidence]) -> None:
        name_tokens = self._tokens(entity.name)
        existing = {asset.url for asset in entity.media}
        candidates: list[tuple[float, str, WebEvidence]] = []
        for item in evidence_items:
            for url in item.image_urls:
                if not url.startswith(("http://", "https://")) or url in existing:
                    continue
                lower = unquote(url).lower()
                score = 0.35 + item.confidence * 0.4
                if any(token in lower for token in name_tokens):
                    score += 0.25
                if any(term in lower for term in ["logo", "icon", "sprite", "avatar"]):
                    score -= 0.4
                candidates.append((score, url, item))
        for score, url, item in sorted(candidates, reverse=True)[:4]:
            if score < 0.45:
                continue
            asset = MediaAsset(
                id=f"{entity.id}:production-image:{abs(hash(url))}",
                source=item.source,
                url=url,
                kind="image",
                caption=item.title or entity.display_name or entity.name,
                is_primary=not entity.media,
                copyright_risk="unknown",
                labels=["production_enriched"],
                quality_score=round(min(1.0, score), 3),
            )
            entity.media.append(asset)
            existing.add(url)
        if entity.media and not entity.card.primary_image_url:
            primary = next((asset for asset in entity.media if asset.is_primary), entity.media[0])
            primary.is_primary = True
            entity.card.primary_image_url = primary.url

    def _geocode_verified_address(self, entity: CityEntity) -> None:
        if not entity.address:
            return
        now = time.monotonic()
        wait = max(0.0, 1.1 - (now - self._last_nominatim_at))
        if wait:
            time.sleep(wait)
        self._last_nominatim_at = time.monotonic()
        query = entity.address if "hyderabad" in entity.address.lower() else f"{entity.address}, Hyderabad, Telangana, India"
        try:
            logger.info("production geocode verified address entity_id=%s query=%s", entity.id, query)
            response = self._client.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": query, "format": "jsonv2", "limit": 1, "addressdetails": 1},
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.info("production geocode failed entity_id=%s error=%s", entity.id, exc)
            return
        if not isinstance(payload, list) or not payload:
            return
        best = payload[0]
        display = str(best.get("display_name") or "")
        if "hyderabad" not in display.lower() and "telangana" not in display.lower():
            return
        try:
            entity.latitude = float(best["lat"])
            entity.longitude = float(best["lon"])
        except (KeyError, TypeError, ValueError):
            return
        entity.geo_precision = "verified_address_geocoded"

    def _evidence_matches(self, evidence: WebEvidence, entity: CityEntity) -> bool:
        haystack = " ".join([evidence.title or "", evidence.text, evidence.address or "", evidence.url]).lower()
        if "hyderabad" not in haystack and "telangana" not in haystack:
            return False
        if self._generic_search_entity(entity):
            return False
        name_tokens = self._tokens(entity.name)
        if not name_tokens:
            return False
        hits = sum(1 for token in name_tokens if token in haystack)
        source_bonus = 0.1 if evidence.source in {"zomato", "swiggy", "tripadvisor", "wanderlog", "lbb"} else 0.0
        field_score = sum(
            1
            for value in [
                evidence.address,
                evidence.latitude is not None and evidence.longitude is not None,
                evidence.image_urls,
                evidence.phone,
                evidence.rating,
            ]
            if value
        ) * 0.12
        evidence.confidence = min(1.0, (hits / max(1, len(name_tokens))) * 0.55 + field_score + source_bonus)
        return evidence.confidence >= 0.45

    def _items(self, payload: object) -> list[dict[str, object]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ["data", "results", "items"]:
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
            if isinstance(value, dict):
                out: list[dict[str, object]] = []
                for nested_key in ["web", "images", "news"]:
                    nested = value.get(nested_key)
                    if isinstance(nested, list):
                        out.extend(item for item in nested if isinstance(item, dict))
                if out:
                    return out
        return []

    def _coords_from_url(self, url: str) -> tuple[float, float] | None:
        decoded = unquote(url)
        parsed = urlparse(decoded)
        params = parse_qs(parsed.query)
        for key in ["destination", "query", "q", "ll"]:
            for value in params.get(key, []):
                coords = self._coords_from_text(value)
                if coords:
                    return coords
        for pattern in [
            r"@(-?\d+\.\d+),(-?\d+\.\d+)",
            r"!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)",
            r"!2d(-?\d+\.\d+)!3d(-?\d+\.\d+)",
            r"(-?17\.\d+)\s*,\s*(78\.\d+)",
        ]:
            match = re.search(pattern, decoded)
            if match:
                return self._coord_pair(match.groups())
        return None

    def _coords_from_text(self, text: str) -> tuple[float, float] | None:
        match = re.search(r"\b(17\.\d{4,})\s*,\s*(78\.\d{4,})\b", text)
        return self._coord_pair(match.groups()) if match else None

    def _coord_pair(self, values: tuple[str, ...]) -> tuple[float, float] | None:
        if len(values) < 2:
            return None
        first, second = float(values[0]), float(values[1])
        if 17.0 <= first <= 18.0 and 78.0 <= second <= 79.0:
            return first, second
        if 17.0 <= second <= 18.0 and 78.0 <= first <= 79.0:
            return second, first
        return None

    def _address_from_text(self, text: str) -> str | None:
        patterns = [
            r"(?:address|location)\s*[:\-]\s*([^.\n]{15,220}(?:Hyderabad|Telangana|500\d{3})[^.\n]{0,80})",
            r"((?:Road|Rd|Plot|Shop|Door|H\.?No|Survey|Sy\.?|Street|Lane|Opposite|Near|Beside|Resham|Banjara|Jubilee|Madhapur|Gachibowli)[^.\n]{15,220}(?:Hyderabad|Telangana|500\d{3})[^.\n]{0,80})",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if match:
                value = self._clean(match.group(1)).strip(" ,;-")
                return value if self._valid_address(value) else None
        return None

    def _valid_address(self, value: str) -> bool:
        lower = value.lower()
        if len(value) < 12 or len(value) > 220:
            return False
        if any(term in lower for term in ["====", "eventsgroups", "reset any day", "#main", "github", "pull request"]):
            return False
        has_city = "hyderabad" in lower or "telangana" in lower or re.search(r"\b500\d{3}\b", lower)
        has_address_shape = bool(
            re.search(
                r"\b(?:road|rd|street|lane|plot|shop|floor|opposite|opp|near|beside|hills|madhapur|gachibowli|kondapur|begumpet|charminar|secunderabad)\b",
                lower,
            )
        )
        return bool(has_city and has_address_shape)

    def _address_text(self, address: object) -> str | None:
        if isinstance(address, str):
            return self._clean(address)
        if not isinstance(address, dict):
            return None
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("postalCode"),
            address.get("addressCountry"),
        ]
        value = ", ".join(str(part) for part in parts if part)
        return self._clean(value) or None

    def _opening_text(self, opening: object) -> str | None:
        if isinstance(opening, str):
            return opening
        if isinstance(opening, list):
            return "; ".join(str(item) for item in opening[:7])
        return str(opening) if opening else None

    def _flatten_json(self, node: object) -> list[object]:
        if isinstance(node, list):
            out: list[object] = []
            for item in node:
                out.extend(self._flatten_json(item))
            return out
        if not isinstance(node, dict):
            return []
        out: list[object] = [node]
        graph = node.get("@graph")
        if isinstance(graph, list):
            out.extend(graph)
        return out

    def _image_values(self, image: object) -> list[str]:
        if isinstance(image, str):
            return [image]
        if isinstance(image, list):
            return [str(item) for item in image if isinstance(item, str)]
        if isinstance(image, dict):
            url = image.get("url") or image.get("contentUrl")
            return [str(url)] if url else []
        return []

    def _locality_from_address(self, text: str | None) -> str | None:
        lower = (text or "").lower()
        for locality in LOCALITY_HINTS:
            if locality in lower:
                return locality.title()
        return "Hyderabad" if "hyderabad" in lower else None

    def _search_name(self, name: str) -> str:
        value = re.sub(r"\b(?:review|photos?|image|primary image)\b", " ", name, flags=re.I)
        value = re.sub(r"\s+", " ", value).strip(" -|:")
        return value

    def _generic_search_entity(self, entity: CityEntity) -> bool:
        lower = entity.name.lower().strip()
        if re.search(
            r"^(?:tech|startup|business|ai|data|web|webinar|workshop|founder|investor)\s+(?:meetups?|events?|conferences?|summits?)$",
            lower,
        ):
            return True
        return lower in {"hackathon", "free workshop", "rave party", "party night", "user group"}

    def _source_name(self, url: str) -> str:
        domain = urlparse(url).netloc.lower().replace("www.", "")
        for source in ["zomato", "swiggy", "tripadvisor", "wanderlog", "lbb", "magicpin", "eazydiner", "google"]:
            if source in domain:
                return source
        return domain.split(".")[0] if domain else "web"

    def _blocked_url(self, url: str) -> bool:
        lower = url.lower()
        return any(term in lower for term in ["login", "signup", "privacy", "terms", "mailto:", "tel:"])

    def _fields(self, evidence: WebEvidence) -> list[str]:
        fields = []
        for key in ["address", "latitude", "longitude", "phone", "opening_hours", "price", "rating", "rating_count"]:
            if getattr(evidence, key):
                fields.append(key)
        if evidence.image_urls:
            fields.append("image")
        return fields

    def _slug(self, value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")

    def _tokens(self, value: str) -> set[str]:
        stop = {"the", "and", "cafe", "restaurant", "hyderabad", "telangana", "india", "near", "road"}
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2 and token not in stop}

    def _float(self, value: object) -> float | None:
        try:
            return float(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _int(self, value: object) -> int | None:
        try:
            return int(str(value).replace(",", ""))
        except (TypeError, ValueError):
            return None

    def _firecrawl_headers(self) -> dict[str, str]:
        if settings.firecrawl_api_key:
            return {"Authorization": f"Bearer {settings.firecrawl_api_key}"}
        return {}

    def _clean(self, value: object) -> str:
        return re.sub(r"\s+", " ", str(value or "")).strip()
