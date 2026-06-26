from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from collector.core.models import CityEntity
from collector.core.storage import JsonStore


MOOD_KEYWORDS: dict[str, list[str]] = {
    "quiet": ["quiet", "quite", "calm", "peaceful", "silent", "serene", "less crowded"],
    "good_for_walking": ["walk", "walking", "trail", "path", "promenade", "park", "lake", "garden"],
    "outdoor": ["outdoor", "open air", "park", "lake", "garden", "trail"],
    "scenic": ["scenic", "view", "beautiful", "sunset", "lake", "greenery"],
    "family_friendly": ["family", "kids", "children"],
    "nightlife": ["nightlife", "night", "pub", "bar", "club", "midnight"],
    "budget": ["budget", "cheap", "affordable", "street food"],
    "social": ["friends", "group", "hangout", "community"],
}

LOCALITY_COORDINATES: dict[str, tuple[float, float]] = {
    "begumpet": (17.4447, 78.4665),
    "banjara hills": (17.4138, 78.4398),
    "gachibowli": (17.4401, 78.3489),
    "hitec city": (17.4435, 78.3772),
    "jubilee hills": (17.4326, 78.4071),
    "kukatpally": (17.4948, 78.3996),
    "secunderabad": (17.4399, 78.4983),
}


class MobileSearchCard(BaseModel):
    id: str
    name: str
    category: str
    locality: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    image_url: str | None = None
    timings: dict[str, Any] = Field(default_factory=dict)
    rating: float | None = None
    rating_count: int | None = None
    review_count: int = 0
    review_snippets: list[str] = Field(default_factory=list)
    mood_tags: list[str] = Field(default_factory=list)
    intent_tags: list[str] = Field(default_factory=list)
    suitability_scores: dict[str, float] = Field(default_factory=dict)
    distance_km: float | None = None
    relevance_score: float = 0.0
    website: str | None = None
    source_count: int = 0
    matched_terms: list[str] = Field(default_factory=list)


class MobileSearchIndex(BaseModel):
    query: str | None = None
    locality_hint: str | None = None
    user_latitude: float | None = None
    user_longitude: float | None = None
    total_cards: int
    cards: list[MobileSearchCard]


class MobileCardExporter:
    def __init__(self, store: JsonStore) -> None:
        self.store = store

    def export(
        self,
        query: str | None = None,
        limit: int = 100,
        user_latitude: float | None = None,
        user_longitude: float | None = None,
    ) -> MobileSearchIndex:
        query_terms = self._terms(query or "")
        query_mood_tags = self._query_mood_tags(query_terms)
        locality_hint = self._locality_hint(query or "", query_terms)
        locality_center = self._locality_center(locality_hint) if locality_hint else None
        cards = [
            self._card(entity, query_terms, query_mood_tags, locality_hint, locality_center, user_latitude, user_longitude)
            for entity in self.store.iter_entities()
        ]
        cards = [card for card in cards if self._include_card(card, query_terms, query_mood_tags, locality_hint)]
        cards.sort(key=lambda card: (card.relevance_score, -(card.distance_km or 9999)), reverse=True)
        return MobileSearchIndex(
            query=query,
            locality_hint=locality_hint,
            user_latitude=user_latitude,
            user_longitude=user_longitude,
            total_cards=len(cards),
            cards=cards[:limit],
        )

    def write_index(
        self,
        query: str | None = None,
        limit: int = 100,
        user_latitude: float | None = None,
        user_longitude: float | None = None,
        filename: str = "mobile_cards.json",
    ) -> Path:
        index = self.export(query, limit, user_latitude, user_longitude)
        self.store.write_index(filename, index.model_dump(mode="json"))
        return self.store.indexes_root / filename

    def _card(
        self,
        entity: CityEntity,
        query_terms: list[str],
        query_mood_tags: list[str],
        locality_hint: str | None,
        locality_center: tuple[float, float] | None,
        user_latitude: float | None,
        user_longitude: float | None,
    ) -> MobileSearchCard:
        reviews = self._read_json(self.store.entity_dir(entity.id) / "reviews.json", [])
        comments = self._read_json(self.store.entity_dir(entity.id) / "comments.json", [])
        review_texts = [str(item.get("text", "")) for item in reviews + comments if isinstance(item, dict)]
        mood_tags = self._mood_tags(entity, review_texts)
        matched_terms = self._matched_terms(entity, review_texts, mood_tags, query_terms)
        distance_km = self._distance_for(entity, user_latitude, user_longitude, locality_center)
        rating, rating_count = self._rating(entity)
        score = self._score(entity, mood_tags, matched_terms, query_mood_tags, locality_hint, distance_km)
        return MobileSearchCard(
            id=entity.id,
            name=entity.name,
            category=entity.category,
            locality=entity.locality,
            address=entity.address,
            latitude=entity.latitude,
            longitude=entity.longitude,
            image_url=self._image_url(entity),
            timings=entity.timings,
            rating=rating,
            rating_count=rating_count,
            review_count=len(review_texts),
            review_snippets=[self._snippet(text) for text in review_texts[:3] if text.strip()],
            mood_tags=mood_tags,
            intent_tags=entity.metadata.intent_tags,
            suitability_scores=entity.metadata.suitability_scores,
            distance_km=distance_km,
            relevance_score=score,
            website=str(entity.website) if entity.website else None,
            source_count=len({source.source for source in entity.sources}),
            matched_terms=matched_terms,
        )

    def _score(
        self,
        entity: CityEntity,
        mood_tags: list[str],
        matched_terms: list[str],
        query_mood_tags: list[str],
        locality_hint: str | None,
        distance_km: float | None,
    ) -> float:
        if not matched_terms and not query_mood_tags and not locality_hint:
            return round((entity.metadata.popularity_score or 0.0) + len(entity.sources) * 0.03, 3)
        score = len(matched_terms) * 0.9
        score += len(set(mood_tags).intersection(query_mood_tags)) * 1.4
        score += sum(entity.metadata.suitability_scores.get(tag, 0.0) for tag in entity.metadata.intent_tags)
        if locality_hint and self._contains(entity, locality_hint):
            score += 3.0
        if distance_km is not None:
            score += max(0.0, 2.5 - min(distance_km, 10.0) * 0.25)
        if entity.latitude is not None and entity.longitude is not None:
            score += 0.5
        if entity.address:
            score += 0.25
        if self._image_url(entity):
            score += 0.25
        return round(score, 3)

    def _mood_tags(self, entity: CityEntity, review_texts: list[str]) -> list[str]:
        text = self._search_text(entity, review_texts).lower()
        tags = set(entity.metadata.intent_tags + entity.metadata.atmosphere)
        for tag, keywords in MOOD_KEYWORDS.items():
            if any(self._has_term(text, keyword) for keyword in keywords):
                tags.add(tag)
        if entity.category in {"park", "garden", "water", "viewpoint", "fitness_station"}:
            tags.update({"outdoor", "good_for_walking"})
        return sorted(tags)

    def _matched_terms(
        self,
        entity: CityEntity,
        review_texts: list[str],
        mood_tags: list[str],
        query_terms: list[str],
    ) -> list[str]:
        text = self._search_text(entity, review_texts).lower()
        matched = []
        for term in query_terms:
            term_moods = self._query_mood_tags([term])
            if set(term_moods).intersection(mood_tags) or term in mood_tags or self._has_term(text, term):
                matched.append(term)
        return sorted(set(matched))

    def _search_text(self, entity: CityEntity, review_texts: list[str]) -> str:
        return " ".join(
            [
                entity.name,
                entity.category,
                " ".join(entity.subcategories),
                entity.locality or "",
                entity.address or "",
                entity.description or "",
                " ".join(entity.amenities),
                " ".join(entity.metadata.intent_tags),
                " ".join(entity.metadata.atmosphere),
                " ".join(entity.metadata.topics),
                " ".join(review_texts),
            ]
        )

    def _include_card(
        self,
        card: MobileSearchCard,
        query_terms: list[str],
        query_mood_tags: list[str],
        locality_hint: str | None,
    ) -> bool:
        if not query_terms:
            return True
        if card.matched_terms or set(card.mood_tags).intersection(query_mood_tags):
            return True
        if locality_hint and card.distance_km is not None and card.distance_km <= 8:
            return True
        return False

    def _locality_hint(self, query: str, terms: list[str]) -> str | None:
        near_match = re.search(r"\bnear\s+([a-z][a-z\s-]{2,40})", query.lower())
        if near_match:
            phrase = near_match.group(1)
            phrase = re.split(r"\b(for|with|and|that|which|where)\b", phrase)[0]
            tokens = [token for token in re.findall(r"[a-z0-9]+", phrase) if token not in {"places", "place"}]
            if tokens:
                candidate = " ".join(tokens[:2])
                if candidate in LOCALITY_COORDINATES:
                    return candidate
                return tokens[0]
        if not terms:
            return None
        localities: dict[str, int] = {}
        for entity in self.store.iter_entities():
            for value in [entity.locality, entity.address, entity.name]:
                if value:
                    normalized = self._normalize(value)
                    if normalized:
                        localities[normalized] = localities.get(normalized, 0) + 1
        for term in terms:
            if term in localities:
                return term
        return None

    def _locality_center(self, locality: str | None) -> tuple[float, float] | None:
        if not locality:
            return None
        if locality in LOCALITY_COORDINATES:
            return LOCALITY_COORDINATES[locality]
        points = []
        for entity in self.store.iter_entities():
            if entity.latitude is None or entity.longitude is None:
                continue
            if self._contains(entity, locality):
                points.append((entity.latitude, entity.longitude))
        if not points:
            return None
        return (sum(point[0] for point in points) / len(points), sum(point[1] for point in points) / len(points))

    def _distance_for(
        self,
        entity: CityEntity,
        user_latitude: float | None,
        user_longitude: float | None,
        locality_center: tuple[float, float] | None,
    ) -> float | None:
        if entity.latitude is None or entity.longitude is None:
            return None
        if user_latitude is not None and user_longitude is not None:
            return self._distance_km(user_latitude, user_longitude, entity.latitude, entity.longitude)
        if locality_center:
            return self._distance_km(locality_center[0], locality_center[1], entity.latitude, entity.longitude)
        return None

    def _rating(self, entity: CityEntity) -> tuple[float | None, int | None]:
        scores = [rating.score for rating in entity.ratings if rating.score is not None]
        counts = [rating.count for rating in entity.ratings if rating.count is not None]
        if not scores:
            return None, sum(counts) if counts else None
        return round(sum(scores) / len(scores), 2), sum(counts) if counts else None

    def _image_url(self, entity: CityEntity) -> str | None:
        for source in entity.sources:
            image_urls = source.metadata.get("image_urls")
            if isinstance(image_urls, list):
                for url in image_urls:
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        return url
            result = source.metadata.get("result")
            if isinstance(result, dict):
                image = result.get("image") or result.get("imageUrl")
                if isinstance(image, str) and image.startswith(("http://", "https://")):
                    return image
        return None

    def _contains(self, entity: CityEntity, value: str) -> bool:
        text = " ".join([entity.name, entity.locality or "", entity.address or ""]).lower()
        return self._has_term(text, value)

    def _terms(self, query: str) -> list[str]:
        aliases = {"quite": "quiet", "placed": "place", "nearby": "near"}
        stopwords = {
            "a",
            "an",
            "the",
            "to",
            "for",
            "of",
            "in",
            "on",
            "at",
            "near",
            "me",
            "show",
            "all",
            "place",
            "places",
        }
        terms = []
        for token in re.findall(r"[a-z0-9]+", query.lower()):
            token = aliases.get(token, token)
            if len(token) > 2 and token not in stopwords:
                terms.append(token)
        return terms

    def _query_mood_tags(self, terms: list[str]) -> list[str]:
        tags = set()
        for term in terms:
            for tag, keywords in MOOD_KEYWORDS.items():
                if term == tag or any(term in keyword.split() or keyword.startswith(term) for keyword in keywords):
                    tags.add(tag)
        return sorted(tags)

    def _normalize(self, value: str) -> str:
        tokens = [token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2]
        return " ".join(tokens[:3])

    def _has_term(self, text: str, term: str) -> bool:
        pattern = rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None

    def _snippet(self, text: str) -> str:
        return re.sub(r"\s+", " ", text).strip()[:220]

    def _read_json(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))

    def _distance_km(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        radius_km = 6371.0088
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        d_phi = math.radians(lat2 - lat1)
        d_lambda = math.radians(lon2 - lon1)
        a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
        return round(radius_km * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)
