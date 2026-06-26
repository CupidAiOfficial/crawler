from __future__ import annotations

from collections import Counter
import re

from collector.core.models import CityEntity, EntityMetadata, TextSignal


INTENT_KEYWORDS: dict[str, list[str]] = {
    "quiet": ["quiet", "calm", "peaceful", "silent", "less crowded", "serene"],
    "walking": ["walk", "walking", "trail", "path", "promenade", "lake", "park"],
    "outdoor": ["outdoor", "open air", "park", "lake", "garden", "trail"],
    "scenic": ["scenic", "view", "beautiful", "lake", "sunset", "greenery"],
    "dating": ["date", "romantic", "couple", "cozy", "quiet", "rooftop"],
    "networking": ["founder", "startup", "meetup", "network", "community"],
    "startup": ["startup", "founder", "pitch", "investor", "cowork"],
    "badminton": ["badminton", "shuttle", "court"],
    "football": ["football", "futsal", "turf"],
    "gaming": ["gaming", "arcade", "esports", "board game"],
    "reading": ["book", "library", "reading", "poetry"],
    "spirituality": ["temple", "mosque", "church", "spiritual", "pray"],
    "volunteering": ["volunteer", "ngo", "cause", "donate"],
    "nightlife": ["bar", "club", "pub", "brew", "nightlife", "dj"],
    "family": ["family", "kids", "park", "mall"],
    "student": ["student", "college", "campus", "budget"],
    "travel": ["tourism", "heritage", "museum", "landmark"],
    "luxury": ["luxury", "premium", "fine dining", "resort"],
    "budget": ["budget", "cheap", "affordable", "street food"],
    "solo": ["solo", "quiet", "walk", "work"],
    "friends": ["friends", "group", "hangout", "team"],
}

POSITIVE = {"good", "great", "best", "love", "amazing", "safe", "clean", "friendly", "beautiful"}
NEGATIVE = {"bad", "worst", "crowded", "unsafe", "dirty", "expensive", "slow", "rude"}


class EnrichmentEngine:
    def enrich(self, entity: CityEntity, reviews: list[TextSignal] | None = None) -> EntityMetadata:
        reviews = reviews or []
        text = " ".join(
            [
                entity.name,
                entity.category,
                " ".join(entity.subcategories),
                entity.description or "",
                entity.locality or "",
                " ".join(entity.amenities),
                " ".join(signal.text for signal in reviews),
            ]
        ).lower()
        metadata = entity.metadata.model_copy(deep=True)
        metadata.intent_tags = self.intent_tags(text)
        metadata.suitability_scores = {
            tag: self.score_tag(tag, text, entity, reviews) for tag in metadata.intent_tags
        }
        metadata.topics = self.topics(text)
        metadata.sentiment = self.sentiment(text)
        metadata.crowd_type = self.crowd_type(text)
        metadata.atmosphere = self.atmosphere(text)
        metadata.hidden_gem_score = self.hidden_gem_score(entity)
        metadata.popularity_score = self.popularity_score(entity, reviews)
        metadata.source_counts = Counter(record.source for record in entity.sources)
        return metadata

    def intent_tags(self, text: str) -> list[str]:
        tags = []
        for tag, keywords in INTENT_KEYWORDS.items():
            if any(self._contains_term(text, keyword) for keyword in keywords):
                tags.append(tag)
        return tags or ["explore"]

    def score_tag(self, tag: str, text: str, entity: CityEntity, reviews: list[TextSignal]) -> float:
        keywords = INTENT_KEYWORDS.get(tag, [])
        hits = sum(self._term_count(text, keyword) for keyword in keywords)
        rating_bonus = 0.0
        if entity.ratings:
            scores = [rating.score / (rating.scale or 5.0) for rating in entity.ratings if rating.score]
            rating_bonus = sum(scores) / len(scores) if scores else 0.0
        review_bonus = min(len(reviews), 20) / 100
        return round(min(1.0, 0.25 + hits * 0.12 + rating_bonus * 0.35 + review_bonus), 3)

    def topics(self, text: str) -> list[str]:
        tokens = [token for token in text.split() if len(token) > 4]
        common = Counter(tokens).most_common(12)
        return [token for token, _ in common]

    def sentiment(self, text: str) -> str:
        positive = sum(self._term_count(text, word) for word in POSITIVE)
        negative = sum(self._term_count(text, word) for word in NEGATIVE)
        if positive > negative * 1.3:
            return "positive"
        if negative > positive * 1.3:
            return "negative"
        return "mixed"

    def crowd_type(self, text: str) -> list[str]:
        crowd = []
        for label in ["students", "families", "founders", "tourists", "couples", "friends"]:
            if label.rstrip("s") in text or label in text:
                crowd.append(label)
        return crowd

    def atmosphere(self, text: str) -> list[str]:
        labels = ["quiet", "peaceful", "lively", "crowded", "cozy", "outdoor", "scenic", "premium", "casual", "spiritual"]
        return [label for label in labels if self._contains_term(text, label)]

    def hidden_gem_score(self, entity: CityEntity) -> float:
        rating_count = sum(rating.count or 0 for rating in entity.ratings)
        source_count = len({record.source for record in entity.sources})
        if rating_count == 0:
            return 0.5
        return round(max(0.0, min(1.0, 0.85 - rating_count / 2000 + source_count * 0.03)), 3)

    def popularity_score(self, entity: CityEntity, reviews: list[TextSignal]) -> float:
        rating_count = sum(rating.count or 0 for rating in entity.ratings)
        sources = len({record.source for record in entity.sources})
        return round(min(1.0, rating_count / 1500 + len(reviews) / 300 + sources * 0.05), 3)

    def _contains_term(self, text: str, term: str) -> bool:
        return self._term_count(text, term) > 0

    def _term_count(self, text: str, term: str) -> int:
        escaped = re.escape(term.lower())
        pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
        return len(re.findall(pattern, text.lower()))
