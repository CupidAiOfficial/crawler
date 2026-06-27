from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from collector.core.config import settings
from collector.core.dedupe import EntityResolver
from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id, normalize_text
from collector.core.models import CityEntity, MediaAsset, SourceRecord
from collector.core.refine import RefinementPipeline
from collector.core.storage import JsonStore


logger = logging.getLogger(__name__)


HYDERABAD_BBOX = (17.2169, 78.1599, 17.6078, 78.6506)
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]


@dataclass(frozen=True)
class LocalitySeed:
    name: str
    latitude: float
    longitude: float


LOCALITIES: list[LocalitySeed] = [
    LocalitySeed("Begumpet", 17.4449, 78.4666),
    LocalitySeed("Ameerpet", 17.4375, 78.4483),
    LocalitySeed("Banjara Hills", 17.4149, 78.4347),
    LocalitySeed("Jubilee Hills", 17.4326, 78.4071),
    LocalitySeed("Madhapur", 17.4483, 78.3915),
    LocalitySeed("HITEC City", 17.4504, 78.3808),
    LocalitySeed("Gachibowli", 17.4401, 78.3489),
    LocalitySeed("Kondapur", 17.4622, 78.3568),
    LocalitySeed("Kukatpally", 17.4948, 78.3996),
    LocalitySeed("Miyapur", 17.4933, 78.3915),
    LocalitySeed("Secunderabad", 17.4399, 78.4983),
    LocalitySeed("Himayatnagar", 17.4007, 78.4895),
    LocalitySeed("Abids", 17.3898, 78.4766),
    LocalitySeed("Koti", 17.3850, 78.4867),
    LocalitySeed("Nampally", 17.3847, 78.4701),
    LocalitySeed("Charminar", 17.3616, 78.4747),
    LocalitySeed("Begum Bazaar", 17.3758, 78.4691),
    LocalitySeed("Somajiguda", 17.4255, 78.4580),
    LocalitySeed("Panjagutta", 17.4262, 78.4511),
    LocalitySeed("Khairatabad", 17.4118, 78.4622),
    LocalitySeed("Kompally", 17.5407, 78.4857),
    LocalitySeed("Sainikpuri", 17.4895, 78.5482),
    LocalitySeed("Dilsukhnagar", 17.3687, 78.5247),
    LocalitySeed("LB Nagar", 17.3457, 78.5522),
    LocalitySeed("Uppal", 17.4056, 78.5591),
    LocalitySeed("Mehdipatnam", 17.3944, 78.4421),
    LocalitySeed("Tolichowki", 17.3993, 78.4138),
    LocalitySeed("Manikonda", 17.4056, 78.3787),
    LocalitySeed("Financial District", 17.4149, 78.3391),
    LocalitySeed("Shamshabad", 17.2603, 78.3969),
]


OSM_QUERIES: dict[str, list[tuple[str, str]]] = {
    "restaurants": [("amenity", "restaurant"), ("amenity", "fast_food"), ("amenity", "food_court")],
    "cafes": [("amenity", "cafe"), ("shop", "coffee")],
    "pubs_bars_lounges": [("amenity", "pub"), ("amenity", "bar"), ("amenity", "nightclub")],
    "bakeries": [("shop", "bakery"), ("craft", "bakery")],
    "attractions": [("tourism", "attraction"), ("historic", "*"), ("tourism", "viewpoint")],
    "parks_lakes": [("leisure", "park"), ("leisure", "garden"), ("natural", "water"), ("water", "lake")],
    "religion": [("amenity", "place_of_worship")],
    "museums_culture": [("tourism", "museum"), ("tourism", "gallery"), ("amenity", "arts_centre")],
    "shopping": [("shop", "mall"), ("amenity", "marketplace"), ("shop", "department_store")],
    "theatres": [("amenity", "cinema"), ("amenity", "theatre")],
    "gaming_sports_fitness": [
        ("leisure", "amusement_arcade"),
        ("leisure", "sports_centre"),
        ("leisure", "fitness_centre"),
        ("sport", "*"),
        ("amenity", "gym"),
    ],
    "coworking_startups": [("office", "coworking"), ("amenity", "coworking_space")],
    "education": [("amenity", "college"), ("amenity", "university")],
    "community_ngo": [("office", "ngo"), ("social_facility", "*"), ("community_centre", "*")],
}


CATEGORY_MAP = {
    "restaurant": "restaurant",
    "fast_food": "restaurant",
    "food_court": "restaurant",
    "cafe": "cafe",
    "coffee": "cafe",
    "pub": "pub",
    "bar": "bar",
    "nightclub": "nightlife",
    "bakery": "bakery",
    "attraction": "attraction",
    "viewpoint": "attraction",
    "museum": "museum",
    "gallery": "museum",
    "park": "park",
    "garden": "park",
    "water": "lake",
    "lake": "lake",
    "place_of_worship": "religion",
    "mall": "mall",
    "marketplace": "market",
    "department_store": "shopping",
    "cinema": "theatre",
    "theatre": "theatre",
    "amusement_arcade": "gaming_center",
    "sports_centre": "sports_venue",
    "fitness_centre": "gym",
    "gym": "gym",
    "coworking": "coworking_space",
    "coworking_space": "coworking_space",
    "college": "college",
    "university": "college",
    "ngo": "ngo",
}


SERVING_CATEGORIES = set(CATEGORY_MAP.values()) | {
    "historic",
    "sports_venue",
    "community_space",
}


class StructuredBulkCollector:
    """High-yield structured acquisition path for Hyderabad POIs.

    This intentionally starts from open structured data. Generic web search can
    enrich later, but source pages should not create serving entities directly.
    """

    def __init__(self, store: JsonStore) -> None:
        self.store = store
        self.http = PoliteHttpClient(
            settings.user_agent,
            timeout_seconds=90,
            min_delay_seconds=max(1.0, settings.request_delay_seconds),
            respect_robots=False,
        )
        self.resolver = EntityResolver(name_threshold=0.88, nearby_meters=90)
        self.wikidata_cache: dict[str, dict[str, Any]] = {}
        self.existing = list(store.iter_entities())

    def run(
        self,
        categories: list[str] | None = None,
        limit: int | None = None,
        enrich_wikimedia: bool = True,
        max_wikimedia: int | None = None,
        refine_after: bool = True,
    ) -> dict[str, Any]:
        selected = categories or list(OSM_QUERIES)
        extracted = 0
        saved = 0
        rejected = Counter()
        by_category: Counter[str] = Counter()
        by_locality: Counter[str] = Counter()
        source_counts: Counter[str] = Counter()
        wikimedia_enriched = 0

        logger.info(
            "structured bulk start categories=%s limit=%s enrich_wikimedia=%s",
            ",".join(selected),
            limit or "none",
            enrich_wikimedia,
        )
        for category_group in selected:
            tags = OSM_QUERIES.get(category_group)
            if not tags:
                logger.warning("structured bulk skipped unknown category_group=%s", category_group)
                continue
            logger.info("structured source queried source=openstreetmap category_group=%s tags=%s", category_group, tags)
            remaining = None if limit is None else max(1, limit - saved)
            payload = self._overpass(tags, result_limit=min(1000, remaining or 1000))
            raw_path = self.store.save_raw("openstreetmap_bulk", category_group, payload)
            elements = payload.get("elements", []) if isinstance(payload, dict) else []
            logger.info("structured source extracted source=openstreetmap category_group=%s elements=%s", category_group, len(elements))
            for element in elements:
                if limit is not None and saved >= limit:
                    break
                extracted += 1
                entity, reason = self._entity_from_osm(element, category_group, raw_path)
                if entity is None:
                    rejected[reason or "unknown"] += 1
                    logger.debug("structured rejected source=openstreetmap reason=%s element_id=%s", reason, element.get("id"))
                    continue
                if enrich_wikimedia and (max_wikimedia is None or wikimedia_enriched < max_wikimedia):
                    before_media = len(entity.media)
                    self._enrich_from_wikidata(entity)
                    if len(entity.media) > before_media:
                        wikimedia_enriched += 1
                entity = self._merge_or_new(entity)
                self.store.save_entity(entity)
                self.existing.append(entity)
                saved += 1
                by_category[entity.primary_category or entity.category] += 1
                by_locality[entity.locality or "Hyderabad"] += 1
                for source in entity.sources:
                    source_counts[source.source] += 1
                logger.info(
                    "structured saved entity=%s category=%s locality=%s images=%s sources=%s progress=%s/%s",
                    entity.name,
                    entity.primary_category or entity.category,
                    entity.locality,
                    len(entity.media),
                    len(entity.sources),
                    saved,
                    limit or "uncapped",
                )
            if limit is not None and saved >= limit:
                break

        if refine_after:
            logger.info("structured refine pass start")
            RefinementPipeline(self.store, production_web_enrich=False, fetch_open_images=False).run()

        report = {
            "extracted_elements": extracted,
            "saved_entities": saved,
            "rejected": dict(rejected),
            "by_category": dict(by_category),
            "by_locality": dict(by_locality),
            "by_source": dict(source_counts),
            "wikimedia_enriched": wikimedia_enriched,
            "target": 5000,
            "coverage_percent_of_target": round((saved / 5000) * 100, 2),
        }
        self.store.write_index("structured_bulk_report.json", report)
        logger.info("structured bulk complete report=%s", report)
        return report

    def _overpass(self, tags: list[tuple[str, str]], result_limit: int = 1000) -> dict[str, Any]:
        query = self._overpass_query(tags, result_limit=result_limit)
        last_error: Exception | None = None
        for url in OVERPASS_URLS:
            try:
                logger.info("overpass request url=%s", url)
                payload = self.http.get_json(url, params={"data": query})
                if isinstance(payload, dict):
                    return payload
            except Exception as exc:
                logger.warning("overpass endpoint failed url=%s error=%s", url, exc)
                last_error = exc
                time.sleep(5)
        if last_error:
            raise last_error
        return {"elements": []}

    def _overpass_query(self, tags: list[tuple[str, str]], result_limit: int = 1000) -> str:
        south, west, north, east = HYDERABAD_BBOX
        clauses = []
        for key, value in tags:
            if value == "*":
                clauses.append(f'nwr["{key}"]({south},{west},{north},{east});')
            else:
                clauses.append(f'nwr["{key}"="{value}"]({south},{west},{north},{east});')
        return f"""
        [out:json][timeout:180];
        (
          {' '.join(clauses)}
        );
        out center tags {max(1, result_limit)};
        """

    def _entity_from_osm(
        self,
        element: dict[str, Any],
        category_group: str,
        raw_path: str,
    ) -> tuple[CityEntity | None, str | None]:
        tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
        if not tags:
            return None, "missing_tags"
        name = self._name(tags)
        if not name:
            return None, "missing_name"
        if self._non_serving_osm(tags):
            return None, "non_serving_category"
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        if lat is None or lon is None:
            return None, "missing_coordinates"
        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None, "invalid_coordinates"
        locality = self._locality(tags, lat, lon)
        address = self._address(tags) or self._fallback_address(name, locality)
        category = self._category(tags, category_group)
        if category not in SERVING_CATEGORIES:
            return None, "unmapped_category"
        osm_type = element.get("type")
        osm_id = element.get("id")
        source_id = f"{osm_type}/{osm_id}"
        entity = CityEntity(
            id=entity_id(name, locality, lat, lon),
            name=name,
            display_name=name,
            aliases=self._aliases(tags),
            entity_type="place",
            category=category,
            primary_category=category,
            subcategories=self._subcategories(tags),
            source_categories=[category_group],
            description=tags.get("description") or tags.get("operator:type"),
            locality=locality,
            city="Hyderabad",
            state="Telangana",
            country="India",
            address=address,
            postal_code=tags.get("addr:postcode"),
            latitude=lat,
            longitude=lon,
            geo_precision="osm_exact",
            map_url=f"https://www.openstreetmap.org/{source_id}",
            osm_url=f"https://www.openstreetmap.org/{source_id}",
            website=tags.get("website") or tags.get("contact:website"),
            wikidata_url=f"https://www.wikidata.org/wiki/{tags['wikidata']}" if tags.get("wikidata") else None,
            wikipedia_url=self._wikipedia_url(tags),
            opening_hours_raw=tags.get("opening_hours"),
            timings={"opening_hours": tags.get("opening_hours")} if tags.get("opening_hours") else {},
            open_24_hours=str(tags.get("opening_hours", "")).strip() == "24/7",
            contact=self._contact(tags),
            social_links=self._social(tags),
            amenities=self._amenities(tags),
            pricing=self._pricing(tags),
            audience=self._audience(category, tags),
            media=self._media_from_osm_tags(tags, name),
            sources=[
                SourceRecord(
                    source="openstreetmap",
                    url=f"https://www.openstreetmap.org/{source_id}",
                    source_id=source_id,
                    license="ODbL",
                    raw_path=raw_path,
                    source_type="open_data",
                    source_name="OpenStreetMap",
                    canonical_url=f"https://www.openstreetmap.org/{source_id}",
                    crawl_status="success",
                    extraction_confidence=0.92,
                    metadata={"osm_tags": tags, "category_group": category_group},
                )
            ],
            confidence_score=0.82,
            source_count=1,
            raw_json={"osm": {"type": osm_type, "id": osm_id, "tags": tags}},
        )
        self._apply_matching_metadata(entity, tags)
        return entity, None

    def _name(self, tags: dict[str, Any]) -> str | None:
        for key in ("name", "brand", "official_name", "operator"):
            value = str(tags.get(key) or "").strip()
            if value and len(value) >= 3:
                return re.sub(r"\s+", " ", value)
        return None

    def _non_serving_osm(self, tags: dict[str, Any]) -> bool:
        bad_values = {
            "atm",
            "bank",
            "bench",
            "bicycle_parking",
            "bus_station",
            "bus_stop",
            "clinic",
            "doctors",
            "fuel",
            "hospital",
            "parking",
            "pharmacy",
            "police",
            "post_box",
            "post_office",
            "taxi",
            "toilets",
        }
        values = {str(tags.get(key, "")).lower() for key in ("amenity", "highway", "shop", "healthcare")}
        return bool(values.intersection(bad_values))

    def _category(self, tags: dict[str, Any], category_group: str) -> str:
        for key in ("amenity", "tourism", "leisure", "shop", "office", "historic", "natural", "water", "sport"):
            value = str(tags.get(key) or "").lower()
            if value in CATEGORY_MAP:
                return CATEGORY_MAP[value]
            if key == "historic" and value:
                return "historic"
            if key == "sport" and value:
                return "sports_venue"
        if category_group == "community_ngo":
            return "community_space"
        return category_group

    def _locality(self, tags: dict[str, Any], lat: float, lon: float) -> str:
        for key in ("addr:suburb", "addr:neighbourhood", "addr:quarter", "addr:city", "is_in:suburb"):
            value = str(tags.get(key) or "").strip()
            if value and value.lower() not in {"hyderabad", "secunderabad"}:
                return value
        return min(LOCALITIES, key=lambda item: self._distance_m(lat, lon, item.latitude, item.longitude)).name

    def _distance_m(self, a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
        radius = 6371000.0
        phi1 = math.radians(a_lat)
        phi2 = math.radians(b_lat)
        d_phi = math.radians(b_lat - a_lat)
        d_lambda = math.radians(b_lon - a_lon)
        h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))

    def _address(self, tags: dict[str, Any]) -> str | None:
        if tags.get("addr:full"):
            return str(tags["addr:full"]).strip()
        parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:neighbourhood"),
            tags.get("addr:suburb"),
            tags.get("addr:city") or "Hyderabad",
            tags.get("addr:postcode"),
        ]
        address = ", ".join(str(part).strip() for part in parts if str(part or "").strip())
        return address or None

    def _fallback_address(self, name: str, locality: str) -> str:
        return f"{name}, {locality}, Hyderabad, Telangana"

    def _aliases(self, tags: dict[str, Any]) -> list[str]:
        values = []
        for key in ("alt_name", "official_name", "short_name", "name:en", "old_name"):
            value = str(tags.get(key) or "").strip()
            if value:
                values.extend(part.strip() for part in re.split(r";|\|", value) if part.strip())
        return sorted(set(values))

    def _subcategories(self, tags: dict[str, Any]) -> list[str]:
        out = []
        for key in ("amenity", "tourism", "leisure", "shop", "office", "historic", "sport", "cuisine", "religion", "denomination"):
            if tags.get(key):
                out.append(f"{key}:{tags[key]}")
        return out

    def _contact(self, tags: dict[str, Any]) -> dict[str, Any]:
        contact: dict[str, Any] = {}
        phone = tags.get("contact:phone") or tags.get("phone")
        if phone:
            contact["phone"] = str(phone)
            contact["phone_numbers"] = [str(phone)]
        email = tags.get("contact:email") or tags.get("email")
        if email:
            contact["email"] = str(email)
            contact["emails"] = [str(email)]
        return contact

    def _social(self, tags: dict[str, Any]) -> dict[str, str]:
        keys = ("facebook", "instagram", "twitter", "contact:facebook", "contact:instagram")
        return {key.replace("contact:", ""): str(tags[key]) for key in keys if tags.get(key)}

    def _pricing(self, tags: dict[str, Any]) -> dict[str, Any]:
        pricing: dict[str, Any] = {}
        if tags.get("fee"):
            pricing["fee"] = tags.get("fee")
        if tags.get("payment:cash"):
            pricing["cash"] = tags.get("payment:cash")
        if tags.get("payment:cards"):
            pricing["cards"] = tags.get("payment:cards")
        return pricing

    def _audience(self, category: str, tags: dict[str, Any]) -> list[str]:
        audience = []
        if category in {"park", "lake", "mall", "attraction", "museum", "religion"}:
            audience.extend(["family", "solo", "friends"])
        if category in {"cafe", "coworking_space"}:
            audience.extend(["students", "professionals", "friends"])
        if category in {"pub", "bar", "nightlife"}:
            audience.extend(["friends", "adults", "date"])
        if str(tags.get("wheelchair") or "").lower() in {"yes", "limited"}:
            audience.append("accessibility")
        return sorted(set(audience))

    def _amenities(self, tags: dict[str, Any]) -> list[str]:
        keys = [
            "wheelchair",
            "outdoor_seating",
            "internet_access",
            "wifi",
            "takeaway",
            "delivery",
            "parking",
            "air_conditioning",
            "smoking",
            "toilets",
        ]
        amenities = [f"{key}:{tags[key]}" for key in keys if tags.get(key)]
        cuisine = str(tags.get("cuisine") or "").strip()
        if cuisine:
            amenities.extend(f"cuisine:{item.strip()}" for item in re.split(r";|,", cuisine) if item.strip())
        return sorted(set(amenities))

    def _media_from_osm_tags(self, tags: dict[str, Any], name: str) -> list[MediaAsset]:
        assets: list[MediaAsset] = []
        image = str(tags.get("image") or "").strip()
        if image.startswith(("http://", "https://")):
            assets.append(self._asset(image, "openstreetmap", name, "osm:image", True))
        commons = str(tags.get("wikimedia_commons") or "").strip()
        url = self._commons_url(commons)
        if url:
            assets.append(self._asset(url, "wikimedia_commons", name, commons, not assets))
        return self._unique_media(assets)

    def _wikipedia_url(self, tags: dict[str, Any]) -> str | None:
        value = str(tags.get("wikipedia") or "").strip()
        if not value:
            return None
        if ":" in value:
            lang, title = value.split(":", 1)
            return f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        return f"https://en.wikipedia.org/wiki/{quote(value.replace(' ', '_'))}"

    def _commons_url(self, value: str) -> str | None:
        if not value:
            return None
        value = value.strip()
        if value.startswith(("http://", "https://")):
            return value
        if value.lower().startswith("file:"):
            filename = value.split(":", 1)[1].strip()
            return f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(filename)}?width=1280"
        return None

    def _enrich_from_wikidata(self, entity: CityEntity) -> None:
        qid = self._qid(entity)
        if not qid:
            return
        data = self._wikidata_entity(qid)
        if not data:
            return
        claims = data.get("claims") if isinstance(data.get("claims"), dict) else {}
        image_name = self._wikidata_claim_value(claims, "P18")
        if isinstance(image_name, str):
            url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(image_name)}?width=1280"
            entity.media.append(self._asset(url, "wikidata", entity.name, f"Wikidata P18 {qid}", not entity.media))
        website = self._wikidata_claim_value(claims, "P856")
        if isinstance(website, str) and website.startswith(("http://", "https://")) and not entity.website:
            entity.website = website
        sitelinks = data.get("sitelinks") if isinstance(data.get("sitelinks"), dict) else {}
        enwiki = sitelinks.get("enwiki") if isinstance(sitelinks.get("enwiki"), dict) else None
        if enwiki and not entity.wikipedia_url:
            title = str(enwiki.get("title") or "")
            if title:
                entity.wikipedia_url = f"https://en.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
        if entity.wikipedia_url and not any(asset.source == "wikipedia" for asset in entity.media):
            self._enrich_from_wikipedia_image(entity, entity.wikipedia_url)
        entity.sources.append(
            SourceRecord(
                source="wikidata",
                url=f"https://www.wikidata.org/wiki/{qid}",
                source_id=qid,
                license="CC0",
                source_type="open_data",
                source_name="Wikidata",
                extraction_confidence=0.78,
                metadata={"matched_from": "osm:wikidata"},
            )
        )
        entity.media = self._unique_media(entity.media)
        entity.source_count = len(entity.sources)

    def _qid(self, entity: CityEntity) -> str | None:
        for source in entity.sources:
            tags = source.metadata.get("osm_tags") if isinstance(source.metadata, dict) else None
            if isinstance(tags, dict) and tags.get("wikidata"):
                return str(tags["wikidata"])
        if entity.wikidata_url and "/wiki/" in entity.wikidata_url:
            return entity.wikidata_url.rsplit("/", 1)[-1]
        return None

    def _wikidata_entity(self, qid: str) -> dict[str, Any] | None:
        if qid in self.wikidata_cache:
            return self.wikidata_cache[qid]
        try:
            logger.info("wikidata enrichment qid=%s", qid)
            payload = self.http.get_json(
                "https://www.wikidata.org/w/api.php",
                params={
                    "action": "wbgetentities",
                    "ids": qid,
                    "props": "claims|sitelinks",
                    "format": "json",
                    "origin": "*",
                },
            )
        except Exception as exc:
            logger.warning("wikidata enrichment failed qid=%s error=%s", qid, exc)
            return None
        entities = payload.get("entities") if isinstance(payload, dict) else None
        data = entities.get(qid) if isinstance(entities, dict) else None
        if isinstance(data, dict):
            self.wikidata_cache[qid] = data
            return data
        return None

    def _wikidata_claim_value(self, claims: dict[str, Any], prop: str) -> Any:
        values = claims.get(prop)
        if not isinstance(values, list) or not values:
            return None
        mainsnak = values[0].get("mainsnak") if isinstance(values[0], dict) else None
        datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
        if isinstance(datavalue, dict):
            return datavalue.get("value")
        return None

    def _enrich_from_wikipedia_image(self, entity: CityEntity, wikipedia_url: str) -> None:
        title = wikipedia_url.rsplit("/wiki/", 1)[-1].replace("_", " ")
        if not title:
            return
        try:
            logger.info("wikipedia pageimage lookup title=%s", title)
            payload = self.http.get_json(
                "https://en.wikipedia.org/w/api.php",
                params={
                    "action": "query",
                    "prop": "pageimages|extracts",
                    "titles": title,
                    "pithumbsize": 1280,
                    "exintro": 1,
                    "explaintext": 1,
                    "format": "json",
                    "origin": "*",
                },
            )
        except Exception as exc:
            logger.debug("wikipedia image lookup failed title=%s error=%s", title, exc)
            return
        pages = (payload.get("query") or {}).get("pages") if isinstance(payload, dict) else None
        if not isinstance(pages, dict):
            return
        for page in pages.values():
            if not isinstance(page, dict):
                continue
            thumb = page.get("thumbnail") if isinstance(page.get("thumbnail"), dict) else {}
            url = str(thumb.get("source") or "")
            if url:
                entity.media.append(self._asset(url, "wikipedia", entity.name, title, not entity.media))
            extract = str(page.get("extract") or "").strip()
            if extract and not entity.description:
                entity.description = extract[:700]

    def _asset(self, url: str, source: str, name: str, caption: str, primary: bool) -> MediaAsset:
        digest = normalize_text(f"{source} {url}")[:80].replace(" ", "-") or "image"
        return MediaAsset(
            id=f"{digest}",
            source=source,
            url=url,
            kind="image",
            caption=caption or name,
            alt_text=name,
            is_primary=primary,
            copyright_risk="open_license_candidate" if source in {"wikidata", "wikipedia", "wikimedia_commons"} else "source_provided",
            metadata={"open_source_image": source in {"wikidata", "wikipedia", "wikimedia_commons"}},
        )

    def _unique_media(self, assets: list[MediaAsset]) -> list[MediaAsset]:
        out: list[MediaAsset] = []
        seen: set[str] = set()
        for asset in assets:
            key = asset.url.lower()
            if key not in seen:
                out.append(asset)
                seen.add(key)
        if out:
            out[0].is_primary = True
        return out

    def _apply_matching_metadata(self, entity: CityEntity, tags: dict[str, Any]) -> None:
        category = entity.primary_category or entity.category
        base_tags = {
            "restaurant": ["food", "friends", "family", "date", "casual"],
            "cafe": ["coffee", "quiet", "work", "reading", "date", "friends"],
            "pub": ["nightlife", "friends", "music", "after_work"],
            "bar": ["nightlife", "friends", "date", "after_work"],
            "nightlife": ["nightlife", "music", "friends", "late_night"],
            "bakery": ["food", "dessert", "casual", "family"],
            "attraction": ["travel", "tourism", "family", "solo", "friends"],
            "historic": ["culture", "tourism", "heritage", "family"],
            "park": ["quiet", "walking", "family", "solo", "friends", "nature"],
            "lake": ["quiet", "walking", "date", "nature", "family"],
            "religion": ["spirituality", "quiet", "family", "culture"],
            "museum": ["culture", "learning", "family", "students"],
            "mall": ["shopping", "family", "friends", "date"],
            "market": ["shopping", "budget", "local", "family"],
            "theatre": ["entertainment", "date", "friends", "family"],
            "gaming_center": ["gaming", "friends", "students"],
            "sports_venue": ["sports", "fitness", "friends"],
            "gym": ["fitness", "health", "solo"],
            "coworking_space": ["startup", "networking", "work", "founders", "professionals"],
            "college": ["students", "learning", "community"],
            "ngo": ["volunteering", "community", "social_impact"],
            "community_space": ["community", "networking", "events"],
        }.get(category, ["explore", "local"])
        amenities = " ".join(entity.amenities).lower()
        if "internet_access" in amenities or "wifi" in amenities:
            base_tags.extend(["wifi", "work"])
        if entity.open_24_hours or "24/7" in str(tags.get("opening_hours") or ""):
            base_tags.extend(["late_night", "after_midnight"])
            entity.late_night = True
            entity.after_midnight = True
        entity.metadata.intent_tags = sorted(set(base_tags))
        entity.metadata.context_keys = sorted(set([category, entity.locality or "Hyderabad", *base_tags]))
        entity.metadata.branch_ids = sorted(set([category, "places", "hyderabad"]))
        entity.metadata.suitability_scores = {tag: 0.72 for tag in entity.metadata.intent_tags}
        entity.metadata.source_quality_score = 0.9
        entity.metadata.data_completeness_score = 0.75
        entity.metadata.trust_signals = ["structured_open_data", "osm_coordinates"]
        entity.card.title = entity.display_name or entity.name
        entity.card.subtitle = f"{category.replace('_', ' ').title()} in {entity.locality or 'Hyderabad'}"
        entity.card.address_short = entity.address
        entity.card.timing_label = entity.opening_hours_raw
        entity.card.mood_tags = entity.metadata.intent_tags[:6]

    def _merge_or_new(self, incoming: CityEntity) -> CityEntity:
        match = self.resolver.find_match(incoming, self.existing)
        if not match:
            return incoming
        logger.info("structured dedupe merge incoming=%s canonical=%s", incoming.name, match.name)
        return self.resolver.merge(match, incoming)
