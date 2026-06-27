from __future__ import annotations

import math
from difflib import SequenceMatcher

from collector.core.ids import normalize_text
from collector.core.models import CityEntity


def geo_distance_m(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    radius = 6371000.0
    phi1 = math.radians(a_lat)
    phi2 = math.radians(b_lat)
    d_phi = math.radians(b_lat - a_lat)
    d_lambda = math.radians(b_lon - a_lon)
    h = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * radius * math.atan2(math.sqrt(h), math.sqrt(1 - h))


class EntityResolver:
    def __init__(self, name_threshold: float = 0.9, nearby_meters: float = 120.0) -> None:
        self.name_threshold = name_threshold
        self.nearby_meters = nearby_meters

    def find_match(self, incoming: CityEntity, existing: list[CityEntity]) -> CityEntity | None:
        best: tuple[float, CityEntity] | None = None
        for entity in existing:
            score = self.similarity(incoming, entity)
            if score >= 0.86 and (best is None or score > best[0]):
                best = (score, entity)
        return best[1] if best else None

    def similarity(self, a: CityEntity, b: CityEntity) -> float:
        name_score = self._name_score(a, b)
        website_score = 1.0 if a.website and b.website and str(a.website) == str(b.website) else 0.0
        address_score = SequenceMatcher(None, normalize_text(a.address), normalize_text(b.address)).ratio()
        geo_score = self._geo_score(a, b)
        return max(
            name_score * 0.65 + geo_score * 0.25 + address_score * 0.10,
            name_score * 0.55 + website_score * 0.45,
        )

    def merge(self, canonical: CityEntity, incoming: CityEntity) -> CityEntity:
        merged = canonical.model_copy(deep=True)
        merged.aliases = sorted(set(merged.aliases + incoming.aliases + [incoming.name]) - {merged.name})
        merged.subcategories = sorted(set(merged.subcategories + incoming.subcategories))
        merged.description = merged.description or incoming.description
        merged.locality = merged.locality or incoming.locality
        merged.address = merged.address or incoming.address
        merged.latitude = merged.latitude if merged.latitude is not None else incoming.latitude
        merged.longitude = merged.longitude if merged.longitude is not None else incoming.longitude
        merged.timings.update(incoming.timings)
        merged.contact.update(incoming.contact)
        merged.website = merged.website or incoming.website
        merged.social_links.update(incoming.social_links)
        merged.ratings = merged.ratings + incoming.ratings
        merged.amenities = sorted(set(merged.amenities + incoming.amenities))
        merged.pricing.update(incoming.pricing)
        merged.audience = sorted(set(merged.audience + incoming.audience))
        merged.popularity.update(incoming.popularity)
        merged.related_entities = sorted(set(merged.related_entities + incoming.related_entities))
        merged.media = self._unique_by_id_or_url(merged.media + incoming.media)
        merged.relationships = self._unique_relationships(merged.relationships + incoming.relationships)
        merged.sources = merged.sources + incoming.sources
        merged.primary_category = merged.primary_category or incoming.primary_category
        merged.entity_type = merged.entity_type or incoming.entity_type
        merged.confidence_score = max(
            score for score in [merged.confidence_score, incoming.confidence_score] if score is not None
        ) if any(score is not None for score in [merged.confidence_score, incoming.confidence_score]) else None
        merged.last_seen_at = incoming.last_seen_at
        return merged

    def _name_score(self, a: CityEntity, b: CityEntity) -> float:
        names_a = [a.name] + a.aliases
        names_b = [b.name] + b.aliases
        return max(
            SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()
            for left in names_a
            for right in names_b
        )

    def _geo_score(self, a: CityEntity, b: CityEntity) -> float:
        if None in [a.latitude, a.longitude, b.latitude, b.longitude]:
            return 0.0
        distance = geo_distance_m(a.latitude or 0, a.longitude or 0, b.latitude or 0, b.longitude or 0)
        if distance <= self.nearby_meters:
            return 1.0
        if distance <= 1000:
            return max(0.0, 1.0 - distance / 1000)
        return 0.0

    def _unique_by_id_or_url(self, items):
        seen = set()
        out = []
        for item in items:
            key = getattr(item, "id", None) or getattr(item, "url", None)
            if key and key not in seen:
                out.append(item)
                seen.add(key)
        return out

    def _unique_relationships(self, relationships):
        seen = set()
        out = []
        for rel in relationships:
            key = (rel.subject_id, rel.predicate, rel.object_id)
            if key not in seen:
                out.append(rel)
                seen.add(key)
        return out
