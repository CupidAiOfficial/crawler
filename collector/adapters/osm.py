from __future__ import annotations

import logging
import re
from typing import Any

from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore
from collector.core.structured_extraction import StructuredExtractor


logger = logging.getLogger(__name__)


HYDERABAD_BBOX = (17.2169, 78.1599, 17.6078, 78.6506)

OSM_CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "food": [("amenity", "restaurant"), ("amenity", "cafe"), ("amenity", "fast_food")],
    "nightlife": [("amenity", "bar"), ("amenity", "pub"), ("amenity", "nightclub")],
    "sports": [("leisure", "sports_centre"), ("sport", "*"), ("leisure", "pitch")],
    "entertainment": [("amenity", "cinema"), ("amenity", "theatre"), ("leisure", "amusement_arcade")],
    "culture": [("tourism", "museum"), ("amenity", "arts_centre"), ("historic", "*")],
    "religion": [("amenity", "place_of_worship")],
    "education": [("amenity", "school"), ("amenity", "college"), ("amenity", "university")],
    "tourism": [("tourism", "attraction"), ("tourism", "hotel"), ("historic", "*")],
    "shopping": [("shop", "*"), ("amenity", "marketplace")],
    "coworking": [("office", "coworking"), ("amenity", "coworking_space")],
    "startup": [("office", "company"), ("office", "coworking")],
    "ngo": [("office", "ngo"), ("social_facility", "*")],
    "parks": [("leisure", "park"), ("leisure", "garden")],
    "lakes": [("natural", "water"), ("water", "lake")],
    "museums": [("tourism", "museum")],
    "theaters": [("amenity", "theatre"), ("amenity", "cinema")],
    "fitness": [("leisure", "fitness_centre"), ("sport", "yoga"), ("amenity", "gym")],
}


class OpenStreetMapAdapter(SourceAdapter):
    name = "openstreetmap"

    def __init__(self, http: PoliteHttpClient, store: JsonStore, limit: int = 80) -> None:
        self.http = http
        self.store = store
        self.limit = limit
        self.extractor = StructuredExtractor()

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        tags = self._tags_for_candidate(candidate.value)
        logger.info("searching openstreetmap category=%s depth=%s", candidate.value, candidate.depth)
        query = self._overpass_query(tags)
        payload = self.http.get_json("https://overpass-api.de/api/interpreter", params={"data": query})
        raw_path = self.store.save_raw(self.name, f"{candidate.value}-{candidate.depth}", payload)
        elements = list((payload or {}).get("elements", []))[: self.limit]  # type: ignore[union-attr]
        entities = [self._entity_from_element(element, candidate.value, raw_path) for element in elements]
        entities = [entity for entity in entities if entity is not None]
        enriched: list[CityEntity] = []
        for entity in entities:
            text = " ".join(
                str(part)
                for part in [
                    entity.name,
                    entity.category,
                    " ".join(entity.subcategories),
                    entity.description or "",
                    entity.locality or "",
                    entity.address or "",
                    " ".join(entity.amenities),
                ]
                if part
            )
            entity, reviews, relationships = self.extractor.enrich_from_document(
                entity,
                text=text,
                url=str(entity.website or ""),
                soup=None,
            )
            if reviews:
                self.store.append_reviews(entity.id, reviews)
            if relationships:
                self.store.append_relationships(entity.id, relationships)
            enriched.append(entity)
        entities = enriched
        new_candidates = self._recursive_candidates(entities, candidate)
        logger.info(
            "openstreetmap extracted category=%s entities=%s new_candidates=%s",
            candidate.value,
            len(entities),
            len(new_candidates),
        )
        return entities, new_candidates

    def _tags_for_candidate(self, value: str) -> list[tuple[str, str]]:
        category_tags = OSM_CATEGORY_TAGS.get(value.lower())
        if category_tags:
            return category_tags
        name = re.sub(r"\b(?:hyderabad|secunderabad|telangana|india)\b", "", value, flags=re.I)
        name = re.sub(r"\b(?:exact location|address|timings|near me|location|photos|images|photo|image)\b", "", name, flags=re.I)
        name = re.sub(r"\s+", " ", name).strip()
        return [("name_regex", name or value)]

    def _overpass_query(self, tags: list[tuple[str, str]]) -> str:
        south, west, north, east = HYDERABAD_BBOX
        clauses = []
        for key, value in tags:
            if value == "*":
                clauses.append(f'nwr["{key}"]({south},{west},{north},{east});')
            elif key == "name_regex":
                pattern = re.escape(value)
                clauses.append(f'nwr["name"~"{pattern}",i]({south},{west},{north},{east});')
            else:
                clauses.append(f'nwr["{key}"="{value}"]({south},{west},{north},{east});')
        joined = "\n  ".join(clauses)
        return f"""
        [out:json][timeout:45];
        (
          {joined}
        );
        out center tags {self.limit};
        """

    def _entity_from_element(
        self, element: dict[str, Any], requested_category: str, raw_path: str
    ) -> CityEntity | None:
        tags = element.get("tags") or {}
        name = tags.get("name") or tags.get("brand") or tags.get("operator")
        if not name:
            return None
        lat = element.get("lat") or (element.get("center") or {}).get("lat")
        lon = element.get("lon") or (element.get("center") or {}).get("lon")
        locality = tags.get("addr:suburb") or tags.get("addr:neighbourhood") or tags.get("addr:city")
        address = self._address(tags) or self._fallback_address(name, locality, tags) if lat and lon else self._address(tags)
        category = self._category(tags, requested_category)
        source_id = f"{element.get('type')}/{element.get('id')}"
        return CityEntity(
            id=entity_id(name, locality, lat, lon),
            name=name,
            aliases=[tags[key] for key in ["alt_name", "official_name", "short_name"] if key in tags],
            category=category,
            subcategories=self._subcategories(tags),
            description=tags.get("description"),
            locality=locality,
            address=address,
            latitude=lat,
            longitude=lon,
            geo_precision="exact" if lat and lon else None,
            timings={"opening_hours": tags.get("opening_hours")} if tags.get("opening_hours") else {},
            contact={
                key: tags[value]
                for key, value in {
                    "phone": "phone",
                    "email": "email",
                    "operator": "operator",
                }.items()
                if value in tags
            },
            website=tags.get("website"),
            social_links=self._social(tags),
            amenities=self._amenities(tags),
            sources=[
                SourceRecord(
                    source=self.name,
                    url=f"https://www.openstreetmap.org/{source_id}",
                    source_id=source_id,
                    license="ODbL",
                    raw_path=raw_path,
                    source_type="open_data",
                    source_name="OpenStreetMap",
                    canonical_url=f"https://www.openstreetmap.org/{source_id}",
                    crawl_status="success",
                    extraction_confidence=0.8,
                    metadata={"osm_tags": tags},
                )
            ],
            raw_json={"address_fallback": address if address and not self._address(tags) else None},
        )

    def _address(self, tags: dict[str, str]) -> str | None:
        parts = [
            tags.get("addr:housenumber"),
            tags.get("addr:street"),
            tags.get("addr:neighbourhood"),
            tags.get("addr:suburb"),
            tags.get("addr:city"),
            tags.get("addr:postcode"),
        ]
        value = ", ".join(part for part in parts if part)
        return value or tags.get("addr:full")

    def _fallback_address(self, name: str, locality: str | None, tags: dict[str, str]) -> str | None:
        parts = [
            name,
            locality,
            tags.get("addr:city") or "Hyderabad",
            tags.get("addr:state") or "Telangana",
        ]
        return ", ".join(dict.fromkeys(str(part) for part in parts if part))

    def _category(self, tags: dict[str, str], requested: str) -> str:
        for key in ["amenity", "tourism", "leisure", "shop", "office", "historic", "sport", "natural", "highway"]:
            if key in tags:
                return tags[key]
        return requested if requested.lower() in OSM_CATEGORY_TAGS else "place"

    def _subcategories(self, tags: dict[str, str]) -> list[str]:
        return [
            f"{key}:{value}"
            for key, value in tags.items()
            if key in {"amenity", "tourism", "leisure", "shop", "office", "historic", "sport", "cuisine"}
        ]

    def _social(self, tags: dict[str, str]) -> dict[str, str]:
        return {
            key.replace("contact:", ""): value
            for key, value in tags.items()
            if key.startswith("contact:") or key in {"facebook", "instagram", "twitter"}
        }

    def _amenities(self, tags: dict[str, str]) -> list[str]:
        keys = ["wheelchair", "outdoor_seating", "internet_access", "parking", "delivery", "takeaway"]
        return [f"{key}:{tags[key]}" for key in keys if key in tags]

    def _recursive_candidates(
        self, entities: list[CityEntity], candidate: CrawlCandidate
    ) -> list[CrawlCandidate]:
        discovered: list[CrawlCandidate] = []
        for entity in entities:
            if entity.website:
                discovered.append(
                    CrawlCandidate(
                        kind=CandidateKind.SOURCE_URL,
                        value=str(entity.website),
                        source="web_page",
                        priority=max(0.1, candidate.priority - 0.1),
                        depth=candidate.depth + 1,
                        metadata={"entity_id": entity.id, "entity_name": entity.name},
                    )
                )
            discovered.append(
                CrawlCandidate(
                    kind=CandidateKind.QUERY,
                    value=entity.name,
                    source="wikipedia",
                    priority=0.35,
                    depth=candidate.depth + 1,
                    metadata={"entity_id": entity.id},
                )
            )
        return discovered
