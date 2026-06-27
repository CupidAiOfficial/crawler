from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

from collector.core.config import settings
from collector.core.models import CityEntity, MediaAsset


logger = logging.getLogger(__name__)


REAL_ENTITY_HINTS = {
    "bar",
    "bazaar",
    "brewery",
    "cafe",
    "café",
    "church",
    "club",
    "fort",
    "garden",
    "lake",
    "lounge",
    "mall",
    "mandir",
    "market",
    "museum",
    "palace",
    "park",
    "planetarium",
    "restaurant",
    "stadium",
    "temple",
    "theatre",
    "tombs",
}

WEAK_PLACE_HINTS = {
    "cohort",
    "conference",
    "conferences",
    "event",
    "events",
    "meetup",
    "meetups",
    "summit",
    "webinar",
    "workshop",
    "workshops",
}

GENERIC_PAGE_TERMS = {
    "about",
    "address",
    "blog",
    "booking",
    "cancellation",
    "contact",
    "directions",
    "general",
    "guide",
    "history",
    "overview",
    "package",
    "packages",
    "payment",
    "policy",
    "privacy",
    "quote",
    "read",
    "terms",
    "timing",
    "timings",
    "trip",
    "trips",
}

GEO_ACCEPT_TERMS = {"hyderabad", "telangana", "secunderabad"}

NON_CARD_CATEGORIES = {
    "bus_stop",
    "bus_station",
    "car",
    "clinic",
    "crossing",
    "cycleway",
    "bank",
    "doctors",
    "footway",
    "hospital",
    "parking",
    "path",
    "police",
    "primary",
    "primary_link",
    "residential",
    "road",
    "secondary",
    "secondary_link",
    "service",
    "tertiary",
    "tertiary_link",
    "ticket",
    "track",
    "trunk",
    "unclassified",
    "yes",
}

COLLECTION_PATTERNS = [
    r"\b(?:events|places|restaurants|cafes|clubs|pubs|bars|things|activities|meetups|conferences|workshops|webinars)\s+in\s+hyderabad\b",
    r"\b(?:hyderabad)\s+(?:events|places|restaurants|cafes|clubs|pubs|bars|things|activities|meetups|conferences|workshops|webinars)\b",
    r"^(?:top|best|popular|upcoming|latest|all)\b",
    r"\b(?:list|guide|collection|calendar|category|directory)\b",
]

INSTRUCTION_PATTERNS = [
    r"^(?:add|apply|create|submit|register|sign\s*up|login|log\s*in|join|follow|share|save|view|read|learn|discovering)\b",
    r"^(?:go to|in hyderabad|adjacent to|located in|near|near to|close to)\b",
    r"^(?:why|what|how|where|when|who)\b",
    r"\b(?:logo|badge|primary image|image number|contribution instructions|privacy policy|terms|cancellation policy|user profile)\b",
    r"\.(?:webp|jpg|jpeg|png|svg|gif|json)\b",
]


@dataclass
class RefinementDecision:
    decision: str
    score: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "decision": self.decision,
            "score": round(self.score, 3),
            "reasons": self.reasons,
        }


class EntityRefiner:
    """Turns raw extracted candidates into app-usable city entities.

    This is intentionally evidence-scored instead of a single brittle rule. It
    combines entity-name shape, source evidence, category, media, locality, and
    geocodeability into a decision that is saved on the entity for audit.
    """

    def __init__(self, fetch_open_images: bool = True) -> None:
        self.fetch_open_images = fetch_open_images
        self._client = httpx.Client(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": settings.user_agent},
        )
        self._last_geocode_at = 0.0

    def refine(self, entity: CityEntity) -> CityEntity:
        previous_name = entity.name
        entity.name = self._clean_name(entity.name)
        if not entity.display_name or entity.display_name == previous_name:
            entity.display_name = entity.name
        self._promote_address_from_name(entity)
        if entity.address and self._invalid_address(entity.address):
            entity.raw_json["refinement_invalid_address"] = entity.address
            entity.address = None
            if entity.geo_precision in {"source_page_coordinates", "verified_address_geocoded"}:
                entity.latitude = None
                entity.longitude = None
                entity.geo_precision = "unresolved"
        decision = self.decide(entity)
        entity.raw_json["refinement"] = decision.as_dict()
        entity.metadata.content_quality_score = decision.score
        entity.metadata.needs_human_review = decision.decision != "serve"
        entity.metadata.review_reason = ", ".join(decision.reasons[:4]) if decision.reasons else None
        if decision.decision == "reject_page_fragment":
            entity.status = "REJECTED_PAGE_FRAGMENT"
            return entity
        if entity.latitude is None or entity.longitude is None or not entity.address:
            self._try_geocode(entity, decision)
        if self.fetch_open_images and not entity.media and not entity.card.primary_image_url:
            self._try_open_image(entity)
        decision = self.decide(entity)
        entity.raw_json["refinement"] = decision.as_dict()
        entity.metadata.content_quality_score = decision.score
        if decision.decision == "reject_page_fragment":
            entity.status = "REJECTED_PAGE_FRAGMENT"
        elif decision.decision == "serve":
            entity.status = "ACTIVE"
        else:
            entity.status = "NEEDS_ENRICHMENT"
        return entity

    def decide(self, entity: CityEntity) -> RefinementDecision:
        score = 0.0
        reasons: list[str] = []
        name = entity.name.strip()
        lower = name.lower()
        words = re.findall(r"[a-z0-9]+", lower)
        category = (entity.primary_category or entity.category or "").strip().lower()
        source_names = {source.source for source in entity.sources}
        source_types = {source.source_type for source in entity.sources if source.source_type}
        single_page_mention = "source_page_mention" in source_types and len(source_names) == 1
        strong_identity = self._has_strong_identity_evidence(entity)
        generic_collection = self._is_generic_collection(lower)

        if not name or len(name) < 3:
            return RefinementDecision("reject_page_fragment", 0.0, ["empty_or_short_name"])
        if category in NON_CARD_CATEGORIES:
            return RefinementDecision("reject_page_fragment", 0.12, ["non_card_map_infrastructure"])
        if self._is_instruction_or_article_phrase(lower):
            return RefinementDecision("reject_page_fragment", 0.08, ["instruction_article_or_page_furniture"])
        if self._is_bad_geocoded_fragment(entity):
            return RefinementDecision("reject_page_fragment", 0.1, ["bad_fragment_geocoded_from_page_text"])
        if single_page_mention and len(words) > 6 and not entity.address and entity.latitude is None:
            return RefinementDecision("reject_page_fragment", 0.2, ["sentence_like_page_mention_without_location"])
        if generic_collection and not strong_identity:
            return RefinementDecision("reject_page_fragment", 0.16, ["collection_or_search_result_heading"])
        if self._is_abstract_topic(lower, category) and not strong_identity:
            return RefinementDecision("reject_page_fragment", 0.18, ["abstract_topic_not_entity"])
        if self._is_generic_event_name(lower) and not strong_identity:
            return RefinementDecision("reject_page_fragment", 0.18, ["generic_event_or_topic_name"])
        if len(words) > 8:
            score -= 0.35
            reasons.append("long_phrase_name")
        if self._looks_like_page_fragment(lower):
            score -= 0.55
            reasons.append("page_or_navigation_phrase")
        if lower.startswith(("address:", "timings:", "entry fee")):
            score -= 0.75
            reasons.append("field_label_extracted_as_name")
        if "?" in name:
            score -= 0.35
            reasons.append("question_title")

        if self._has_real_entity_hint(lower):
            score += 0.3
            reasons.append("real_entity_name_hint")
        if strong_identity:
            score += 0.26
            reasons.append("strong_identity_evidence")
        if entity.category and entity.category not in {"place", "web_entity"}:
            score += 0.08
            reasons.append("specific_category")
        if entity.locality:
            score += 0.12
            reasons.append("has_locality")
        if entity.address:
            score += 0.18
            reasons.append("has_address")
        if entity.latitude is not None and entity.longitude is not None:
            score += 0.28
            reasons.append("has_coordinates")
        if entity.media or entity.card.primary_image_url:
            score += 0.04
            reasons.append("has_media")
        if entity.website:
            score += 0.08
            reasons.append("has_website")

        if source_names.intersection({"openstreetmap", "wikidata", "wikipedia"}):
            score += 0.25
            reasons.append("trusted_structured_source")
        if single_page_mention:
            score -= 0.24
            reasons.append("single_source_page_mention")

        if len(words) == 1 and not self._has_real_entity_hint(lower):
            score -= 0.18
            reasons.append("single_generic_token")
        if generic_collection:
            score -= 0.35
            reasons.append("generic_collection_heading")

        score = max(0.0, min(1.0, 0.45 + score))
        if single_page_mention and not strong_identity and not self._has_real_entity_hint(lower):
            score = min(score, 0.59)
            reasons.append("insufficient_identity_for_single_page_mention")
        if score < 0.42:
            decision = "reject_page_fragment"
        elif score < 0.72 or not strong_identity:
            decision = "needs_enrichment"
        else:
            decision = "serve"
        return RefinementDecision(decision, score, reasons)

    def _try_geocode(self, entity: CityEntity, decision: RefinementDecision) -> None:
        if decision.decision == "reject_page_fragment":
            return
        if not self._should_geocode(entity, decision):
            return
        query = self._geocode_query(entity)
        if not query:
            return
        now = time.monotonic()
        elapsed = now - self._last_geocode_at
        if elapsed < 1.1:
            time.sleep(1.1 - elapsed)
        self._last_geocode_at = time.monotonic()
        try:
            logger.info("geocode lookup entity_id=%s query=%s", entity.id, query)
            response = self._client.get(
                "https://nominatim.openstreetmap.org/search",
                params={
                    "q": query,
                    "format": "jsonv2",
                    "limit": 1,
                    "addressdetails": 1,
                    "bounded": 1,
                    "viewbox": "78.1599,17.6078,78.6506,17.2169",
                },
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.warning("geocode failed entity_id=%s query=%s error=%s", entity.id, query, exc)
            return
        if not isinstance(payload, list) or not payload:
            return
        best = payload[0]
        display = str(best.get("display_name") or "")
        if not any(term in display.lower() for term in GEO_ACCEPT_TERMS):
            logger.info("geocode rejected outside city entity_id=%s display=%s", entity.id, display)
            return
        try:
            entity.latitude = float(best["lat"])
            entity.longitude = float(best["lon"])
        except (KeyError, TypeError, ValueError):
            return
        entity.address = entity.address or display
        entity.locality = entity.locality or self._locality_from_address(best.get("address"))
        entity.locality = entity.locality or "Hyderabad"
        entity.geo_precision = "geocoded"
        entity.raw_json["geocode"] = {
            "provider": "nominatim",
            "query": query,
            "display_name": display,
            "importance": best.get("importance"),
            "class": best.get("class"),
            "type": best.get("type"),
        }

    def _geocode_query(self, entity: CityEntity) -> str | None:
        name = entity.name.strip()
        if self._looks_like_page_fragment(name.lower()):
            return None
        parts = [name, entity.locality, "Hyderabad", "Telangana", "India"]
        return ", ".join(dict.fromkeys(str(part) for part in parts if part))

    def _try_open_image(self, entity: CityEntity) -> None:
        if not self._should_fetch_open_image(entity):
            return
        title = quote(entity.name.strip().replace(" ", "_"))
        try:
            logger.info("open image lookup entity_id=%s title=%s", entity.id, entity.name)
            response = self._client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}")
            if response.status_code == 404:
                return
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            logger.debug("open image lookup failed entity_id=%s name=%s error=%s", entity.id, entity.name, exc)
            return
        if not isinstance(payload, dict):
            return
        if str(payload.get("type") or "").lower() == "disambiguation":
            return
        extract = str(payload.get("extract") or "").lower()
        page_title = str(payload.get("title") or "")
        if not self._image_page_matches_entity(entity, page_title, extract):
            return
        thumbnail = payload.get("thumbnail") if isinstance(payload.get("thumbnail"), dict) else {}
        original = payload.get("originalimage") if isinstance(payload.get("originalimage"), dict) else {}
        image_url = str(original.get("source") or thumbnail.get("source") or "")
        if not image_url:
            return
        asset = MediaAsset(
            id=f"{entity.id}:wikipedia-summary-image",
            source="wikipedia",
            url=image_url,
            kind="image",
            caption=page_title or entity.name,
            is_primary=True,
            copyright_risk="open_license_candidate",
            metadata={
                "provider": "wikipedia_rest_summary",
                "page_title": page_title,
                "page_url": (payload.get("content_urls") or {}).get("desktop", {}).get("page")
                if isinstance(payload.get("content_urls"), dict)
                else None,
            },
        )
        entity.media.append(asset)
        entity.card.primary_image_url = image_url
        entity.raw_json["open_image_enrichment"] = {
            "provider": "wikipedia_rest_summary",
            "page_title": page_title,
            "url": image_url,
        }

    def _should_fetch_open_image(self, entity: CityEntity) -> bool:
        category = (entity.primary_category or entity.category or "").strip().lower()
        if category in NON_CARD_CATEGORIES:
            return False
        if entity.status == "REJECTED_PAGE_FRAGMENT":
            return False
        if not (entity.latitude is not None and entity.longitude is not None and entity.address):
            return False
        lower = entity.name.lower()
        if self._is_instruction_or_article_phrase(lower) or self._is_generic_collection(lower):
            return False
        return self._has_strong_identity_evidence(entity) or self._has_real_entity_hint(lower)

    def _image_page_matches_entity(self, entity: CityEntity, page_title: str, extract: str) -> bool:
        title_tokens = set(re.findall(r"[a-z0-9]+", page_title.lower()))
        entity_tokens = {
            token
            for token in re.findall(r"[a-z0-9]+", entity.name.lower())
            if len(token) > 2 and token not in {"the", "and", "near", "hyderabad", "telangana", "india"}
        }
        if entity_tokens and len(entity_tokens.intersection(title_tokens)) >= min(2, len(entity_tokens)):
            return True
        city_terms = {"hyderabad", "telangana", "india"}
        if entity_tokens and entity_tokens.intersection(title_tokens) and city_terms.intersection(set(re.findall(r"[a-z0-9]+", extract))):
            return True
        return False

    def _clean_name(self, value: str) -> str:
        value = re.sub(r"\s+", " ", value or "").strip()
        value = re.sub(r"^\s*\d+\s*(?:[.)-]|\.\s*)\s*", "", value)
        value = re.sub(r"\s+\|\s+.*$", "", value)
        value = re.sub(r"\s+-\s+(?:Hyderabad|Telangana|Review|Timings|Entry Fee).*$", "", value, flags=re.I)
        return value.strip(" -:|")

    def _promote_address_from_name(self, entity: CityEntity) -> None:
        match = re.match(r"address\s*:\s*(.+)$", entity.name, flags=re.I)
        if not match:
            return
        entity.address = entity.address or match.group(1).strip()
        entity.raw_json["refinement_address_from_name"] = True

    def _looks_like_page_fragment(self, lower_name: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", lower_name))
        if tokens.intersection(GENERIC_PAGE_TERMS):
            if not self._has_real_entity_hint(lower_name):
                return True
        if lower_name.startswith(("about ", "how to ", "where to ", "what to ")):
            return True
        if self._is_instruction_or_article_phrase(lower_name):
            return True
        return False

    def _has_real_entity_hint(self, lower_name: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", lower_name))
        return bool(tokens.intersection(REAL_ENTITY_HINTS))

    def _has_strong_identity_evidence(self, entity: CityEntity) -> bool:
        lower = entity.name.lower()
        category = (entity.primary_category or entity.category or "").strip().lower()
        source_names = {source.source for source in entity.sources}
        source_types = {source.source_type for source in entity.sources if source.source_type}
        single_page_mention = "source_page_mention" in source_types and len(source_names) == 1
        if self._is_single_page_geocode_only(entity):
            return False
        if single_page_mention and entity.latitude is None and not entity.address:
            if not (entity.website or entity.contact or entity.social_links):
                if not (
                    entity.event_details
                    and (
                        entity.event_details.event_start_at
                        or entity.event_details.ticket_url
                        or entity.event_details.venue_name
                        or entity.event_details.organizer_name
                    )
                ):
                    return False
        if entity.latitude is not None and entity.longitude is not None and entity.address:
            return True
        if source_names.intersection({"openstreetmap", "wikidata", "wikipedia"}) and category not in NON_CARD_CATEGORIES:
            return True
        if entity.website and not self._is_generic_collection(lower):
            return True
        if entity.contact or entity.social_links:
            return True
        if entity.event_details:
            details = entity.event_details
            if details.event_start_at or details.ticket_url or details.venue_name or details.organizer_name:
                return not self._is_generic_collection(lower)
        if entity.community_details:
            details = entity.community_details
            if details.member_count or details.meeting_location or details.online_presence or details.joining_method:
                return not self._is_generic_collection(lower)
        if self._has_real_entity_hint(lower) and (entity.locality or entity.address or entity.media):
            return True
        return False

    def _should_geocode(self, entity: CityEntity, decision: RefinementDecision) -> bool:
        if decision.decision == "reject_page_fragment":
            return False
        lower = entity.name.lower()
        category = (entity.primary_category or entity.category or "").strip().lower()
        if category in NON_CARD_CATEGORIES:
            return False
        if self._is_generic_collection(lower) or self._is_instruction_or_article_phrase(lower):
            return False
        if self._has_strong_identity_evidence(entity):
            return True
        if self._has_real_entity_hint(lower) and entity.locality:
            return True
        return False

    def _is_generic_collection(self, lower_name: str) -> bool:
        if any(re.search(pattern, lower_name, flags=re.I) for pattern in COLLECTION_PATTERNS):
            return True
        tokens = set(re.findall(r"[a-z0-9]+", lower_name))
        if tokens.intersection(WEAK_PLACE_HINTS) and "hyderabad" in tokens:
            return True
        if lower_name.endswith((" events", " meetups", " conferences", " workshops", " webinars")):
            return True
        return False

    def _is_instruction_or_article_phrase(self, lower_name: str) -> bool:
        if lower_name.strip() in {"go to", "in hyderabad", "hotels in hyderabad", "health insurance", "life insurance", "mental health"}:
            return True
        if re.search(r"\broad\b", lower_name):
            return True
        return any(re.search(pattern, lower_name, flags=re.I) for pattern in INSTRUCTION_PATTERNS)

    def _is_abstract_topic(self, lower_name: str, category: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", lower_name))
        if category not in {"event", "community", "business", "place", "web_entity"}:
            return False
        if tokens.intersection({"ai", "ml", "data", "analytics", "marketing", "startups", "startup"}):
            if len(tokens) <= 4 and not self._has_real_entity_hint(lower_name):
                return True
        if tokens.intersection({"insurance", "management", "leadership", "vision", "team", "processes"}):
            return True
        return False

    def _is_generic_event_name(self, lower_name: str) -> bool:
        return re.search(
            r"^(?:tech|startup|business|ai|data|web|webinar|workshop|founder|investor)\s+(?:meetups?|events?|conferences?|summits?)$",
            lower_name,
        ) is not None or lower_name in {"hackathon", "free workshop", "rave party", "party night", "user group"}

    def _invalid_address(self, address: str) -> bool:
        lower = address.lower()
        if len(address) > 220:
            return True
        if any(term in lower for term in ["====", "eventsgroups", "reset any day", "#main", "pull request", "github"]):
            return True
        if "hyderabad" not in lower and "telangana" not in lower and not re.search(r"\b500\d{3}\b", lower):
            return True
        return False

    def _is_single_page_geocode_only(self, entity: CityEntity) -> bool:
        source_names = {source.source for source in entity.sources}
        source_types = {source.source_type for source in entity.sources if source.source_type}
        if not ("source_page_mention" in source_types and len(source_names) == 1):
            return False
        if entity.geo_precision != "geocoded":
            return False
        if entity.website or entity.contact or entity.social_links:
            return False
        lower = entity.name.lower()
        if self._has_real_entity_hint(lower):
            return False
        if entity.event_details and (
            entity.event_details.event_start_at
            or entity.event_details.ticket_url
            or entity.event_details.venue_name
            or entity.event_details.organizer_name
        ):
            return False
        return True

    def _is_bad_geocoded_fragment(self, entity: CityEntity) -> bool:
        lower = entity.name.lower()
        if self._is_single_page_geocode_only(entity) and (
            len(re.findall(r"[a-z0-9]+", lower)) <= 3
            or self._is_abstract_topic(lower, (entity.primary_category or entity.category or "").lower())
            or self._is_generic_collection(lower)
        ):
            return True
        return False

    def _locality_from_address(self, address: object) -> str | None:
        if not isinstance(address, dict):
            return None
        for key in ["suburb", "neighbourhood", "city_district", "city", "town"]:
            value = address.get(key)
            if isinstance(value, str) and value:
                return value
        return None
