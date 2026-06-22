from __future__ import annotations

from typing import Any

from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore


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

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.QUERY

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        tags = OSM_CATEGORY_TAGS.get(candidate.value.lower(), [("name", candidate.value)])
        query = self._overpass_query(tags)
        payload = self.http.get_json("https://overpass-api.de/api/interpreter", params={"data": query})
        raw_path = self.store.save_raw(self.name, f"{candidate.value}-{candidate.depth}", payload)
        elements = list((payload or {}).get("elements", []))[: self.limit]  # type: ignore[union-attr]
        entities = [self._entity_from_element(element, candidate.value, raw_path) for element in elements]
        entities = [entity for entity in entities if entity is not None]
        new_candidates = self._recursive_candidates(entities, candidate)
        return entities, new_candidates

    def _overpass_query(self, tags: list[tuple[str, str]]) -> str:
        south, west, north, east = HYDERABAD_BBOX
        clauses = []
        for key, value in tags:
            if value == "*":
                clauses.append(f'nwr["{key}"]({south},{west},{north},{east});')
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
        address = self._address(tags)
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
                    metadata={"osm_tags": tags},
                )
            ],
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

    def _category(self, tags: dict[str, str], requested: str) -> str:
        for key in ["amenity", "tourism", "leisure", "shop", "office", "historic", "sport", "natural"]:
            if key in tags:
                return tags[key]
        return requested

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
