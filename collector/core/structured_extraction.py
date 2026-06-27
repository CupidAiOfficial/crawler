from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Iterable
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from collector.core.models import (
    AppCard,
    CityEntity,
    CommunityDetails,
    EventDetails,
    ExtractionAudit,
    MediaAsset,
    Relationship,
    SourceRecord,
    TextSignal,
)
from collector.core.ids import entity_id


logger = logging.getLogger(__name__)


CONTEXT_RULES: dict[str, list[str]] = {
    "BUILD_NETWORK": ["startup", "founder", "investor", "pitch", "networking", "vc", "angel", "entrepreneur"],
    "COFFEE_WORK": ["coffee", "cafe", "work", "wifi", "cowork", "laptop", "meeting"],
    "DATE_NIGHT": ["date", "romantic", "couple", "rooftop", "cozy", "dinner"],
    "EAT_DRINK": ["restaurant", "food", "biryani", "bar", "pub", "brewery", "cafe"],
    "EXPLORE_CITY": ["heritage", "landmark", "museum", "temple", "fort", "tourism", "attraction"],
    "FRIENDS": ["friends", "hangout", "group", "club", "board game", "community"],
    "LIVE_EVENTS": ["event", "concert", "workshop", "meetup", "screening", "festival", "open mic"],
    "SOLO_RESET": ["quiet", "peaceful", "reading", "library", "walk", "lake", "park", "meditation"],
    "SPORTS_PLAY": ["badminton", "football", "futsal", "cricket", "fitness", "gym", "yoga", "sports"],
    "TONIGHT": ["tonight", "late night", "midnight", "evening", "night"],
}

BRANCH_RULES: dict[str, list[str]] = {
    "investor_meetups": ["investor", "vc", "angel", "funding", "pitch"],
    "startup_networking": ["startup", "founder", "entrepreneur", "networking", "t-hub"],
    "tech_events": ["technology", "tech", "ai", "software", "developer", "hackathon"],
    "quiet_walks": ["quiet", "walk", "walking", "lake", "park", "trail"],
    "late_night_places": ["late night", "midnight", "after midnight", "night"],
    "spiritual_pray": ["temple", "mosque", "church", "spiritual", "pray", "darshan"],
    "coffee_work": ["coffee", "cafe", "wifi", "laptop", "cowork"],
    "date_places": ["date", "romantic", "couple", "cozy"],
    "sports_play": ["badminton", "football", "cricket", "futsal", "sports"],
}

INTENT_KEYWORDS: dict[str, list[str]] = {
    "investors": ["investor", "vc", "angel", "funding", "capital"],
    "founders": ["founder", "cofounder", "entrepreneur"],
    "tech": ["tech", "technology", "software", "ai", "developer", "startup"],
    "networking": ["network", "networking", "meetup", "community"],
    "quiet": ["quiet", "calm", "peaceful", "less crowded", "serene"],
    "walking": ["walk", "walking", "trail", "promenade", "lake", "park"],
    "late_night": ["late night", "midnight", "after midnight", "open late"],
    "date": ["date", "romantic", "couple"],
    "family": ["family", "kids", "children"],
    "budget": ["budget", "cheap", "affordable", "free"],
    "luxury": ["luxury", "premium", "fine dining"],
    "spiritual": ["temple", "spiritual", "pray", "darshan", "mosque", "church"],
    "work_friendly": ["wifi", "laptop", "work friendly", "power outlet", "cowork"],
    "women_friendly": ["women friendly", "safe for women", "well lit", "safe"],
    "first_timer_friendly": ["first timer", "beginner friendly", "friendly staff"],
    "hidden_gem": ["hidden gem", "underrated", "less known"],
}

NEGATIVE_TAGS: dict[str, list[str]] = {
    "too_loud": ["too loud", "noisy", "deafening"],
    "crowded": ["crowded", "packed", "rush"],
    "expensive": ["expensive", "overpriced", "costly"],
    "unsafe": ["unsafe", "shady", "not safe"],
    "poor_parking": ["parking problem", "no parking", "parking issue"],
    "not_quiet": ["not quiet", "very loud"],
}

AMENITY_PATTERNS: dict[str, list[str]] = {
    "wifi": ["wifi", "wi-fi", "internet"],
    "power_outlets": ["power outlet", "charging", "plug point"],
    "parking": ["parking", "valet"],
    "outdoor_seating": ["outdoor seating", "open air"],
    "air_conditioning": ["air conditioned", "ac ", "a/c"],
    "wheelchair_accessible": ["wheelchair", "accessible"],
    "pet_friendly": ["pet friendly", "pets allowed"],
    "alcohol": ["alcohol", "bar", "beer", "cocktail"],
    "live_music": ["live music", "dj", "band"],
    "sports_facility": ["court", "turf", "pitch", "gym"],
    "reservation_required": ["reservation", "booking required", "book in advance"],
}

LOCALITIES = [
    "begumpet",
    "hitec city",
    "madhapur",
    "gachibowli",
    "kondapur",
    "jubilee hills",
    "banjara hills",
    "ameerpet",
    "secunderabad",
    "raidurg",
    "kukatpally",
    "charminar",
    "abids",
]

ARTICLE_TITLE_TERMS = [
    "best",
    "top",
    "places to visit",
    "things to do",
    "weekend getaway",
    "guide",
    "list",
    "hangout places",
    "romantic places",
    "cheap things",
    "cafes in hyderabad",
    "restaurants in hyderabad",
    "where to",
    "what to",
]

BAD_ENTITY_NAME_TERMS = {
    "right-triangle",
    "cross",
    "home",
    "india",
    "hyderabad",
    "telangana",
    "read more",
    "view all",
    "show all",
    "previous",
    "next",
    "login",
    "sign up",
    "privacy policy",
    "terms",
    "restaurants",
    "cafes",
    "things to do",
    "places to visit",
    "about us",
    "contact us",
    "careers",
    "blogs",
    "general",
    "payments",
    "follow us on",
    "get directions",
    "clear directions",
    "view details",
    "read now",
    "request quote",
    "map view",
    "please wait",
    "facebook",
    "instagram",
    "linkedin",
    "twitter",
    "youtube",
    "whatsapp",
}

ENTITY_TYPE_HINTS: dict[str, list[str]] = {
    "place": ["lake", "park", "fort", "palace", "temple", "museum", "road", "garden", "mall", "market", "cafe", "restaurant", "bar", "pub", "brewery", "workspace", "coworking", "stadium", "club"],
    "event": ["event", "festival", "concert", "workshop", "meetup", "screening", "open mic", "conference", "summit"],
    "community": ["community", "club", "group", "ngo", "foundation", "society", "collective"],
}


class StructuredExtractor:
    """Heuristic extractor for the final crawler standard.

    This deliberately avoids pretending to have source-specific privileged data.
    It extracts what public page text/metadata can support, records evidence,
    and leaves unknown fields null for later API/LLM/manual enrichment.
    """

    version = "structured-extractor-v1"

    def is_source_page(self, *, title: str | None, text: str, url: str | None = None) -> bool:
        haystack = self._clean(" ".join([title or "", text[:3000], url or ""])).lower()
        if not haystack:
            return False
        if any(term in haystack for term in ARTICLE_TITLE_TERMS):
            return True
        if re.search(r"\b(?:top|best)\s+\d{1,3}\b", haystack):
            return True
        if len(re.findall(r"\b\d{1,2}\.\s+[A-Z]", text[:8000])) >= 3:
            return True
        return False

    def extract_mentioned_entities(
        self,
        *,
        text: str,
        source_url: str,
        source_title: str | None,
        raw_path: str | None,
        source_name: str,
        source_metadata: dict[str, object] | None = None,
        soup: BeautifulSoup | None = None,
    ) -> list[CityEntity]:
        names = self._candidate_names_from_text(text)
        if soup:
            names.extend(self._candidate_names_from_soup(soup))
        entities: list[CityEntity] = []
        seen: set[str] = set()
        for name in names:
            clean_name = self._clean_entity_name(name)
            if not self._valid_entity_name(clean_name):
                continue
            key = clean_name.lower()
            if key in seen:
                continue
            seen.add(key)
            evidence = self._evidence_window(text, clean_name)
            locality = self._locality_from_text(evidence) or "Hyderabad"
            category = self._category_from_name(clean_name, evidence)
            entity = CityEntity(
                id=entity_id(clean_name, locality),
                name=clean_name,
                display_name=clean_name,
                entity_type=self._entity_type(category),
                category=category,
                primary_category=category,
                description=evidence[:500] or f"Mentioned by {source_title or source_url}",
                summary=evidence[:500] or None,
                locality=locality or "Hyderabad",
                website=None,
                geo_precision="unresolved",
                confidence_score=0.45,
                raw_json={
                    "source_page_title": source_title,
                    "source_page_url": source_url,
                    "extraction_mode": "mentioned_entity",
                },
                sources=[
                    SourceRecord(
                        source=source_name,
                        url=source_url,
                        raw_path=raw_path,
                        source_type="source_page_mention",
                        source_name=urlparse(source_url).netloc,
                        canonical_url=source_url,
                        discovered_from_url=source_url,
                        crawl_status="success",
                        extraction_confidence=0.45,
                        metadata={
                            "source_page_title": source_title,
                            "mention_evidence": evidence[:1000],
                            **(source_metadata or {}),
                        },
                    )
                ],
            )
            entity, _, relationships = self.enrich_from_document(
                entity,
                text=" ".join([clean_name, evidence, source_title or ""]),
                url=source_url,
                soup=None,
                markdown=None,
            )
            if soup:
                entity.media = self._unique_media(entity.media + self._media_for_entity(clean_name, soup, source_url))
            entity.relationships = self._unique_relationships(entity.relationships + relationships)
            entities.append(entity)
            if len(entities) >= 40:
                break
        logger.info("extracted mentioned entities source_url=%s count=%s", source_url, len(entities))
        return entities

    def enrich_from_document(
        self,
        entity: CityEntity,
        *,
        text: str,
        url: str | None = None,
        soup: BeautifulSoup | None = None,
        markdown: str | None = None,
    ) -> tuple[CityEntity, list[TextSignal], list[Relationship]]:
        logger.debug("structured_extract.start entity=%s url=%s", entity.name, url)
        text = self._clean(text)
        lower = text.lower()
        evidence: dict[str, str] = {}
        extracted: set[str] = set()

        entity.display_name = entity.display_name or entity.name
        entity.primary_category = entity.primary_category or entity.category
        entity.summary = entity.summary or self._sentence(text)
        entity.full_text_excerpt = text[:2000] or entity.full_text_excerpt
        entity.page_title = entity.page_title or self._title(soup)
        entity.meta_description = entity.meta_description or self._meta(soup, "description")
        entity.headings = entity.headings or self._headings(soup)
        entity.source_count = len({source.source for source in entity.sources})
        entity.confidence_score = entity.confidence_score or self._confidence(entity, text)

        self._apply_contact(entity, text, soup, url, evidence, extracted)
        self._apply_location(entity, text, evidence, extracted)
        self._apply_timings(entity, text, evidence, extracted)
        self._apply_pricing(entity, text, evidence, extracted)
        self._apply_rating(entity, text, evidence, extracted)
        self._apply_intents(entity, text, evidence, extracted)
        self._apply_atmosphere(entity, text)
        self._apply_amenities(entity, text)
        self._apply_media(entity, soup, url)
        self._apply_event_or_community(entity, text, url, evidence, extracted)
        reviews = self._review_signals(entity, text, url)
        relationships = self._relationships(entity, text, url)
        entity.relationships = self._unique_relationships(entity.relationships + relationships)
        entity.dedupe = self._dedupe(entity, text)
        entity.card = self._card(entity)
        entity.extraction_audit = self._audit(entity, extracted, evidence)
        logger.debug(
            "structured_extract.done entity=%s fields=%s media=%s reviews=%s relationships=%s",
            entity.name,
            len(extracted),
            len(entity.media),
            len(reviews),
            len(relationships),
        )
        return entity, reviews, relationships

    def _apply_contact(
        self,
        entity: CityEntity,
        text: str,
        soup: BeautifulSoup | None,
        url: str | None,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        emails = sorted(set(re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", text)))
        phones = sorted(set(re.findall(r"(?:\+91[-\s]?)?[6-9]\d{9}", text)))
        if emails:
            entity.contact.setdefault("emails", emails)
            evidence["emails"] = ", ".join(emails[:3])
            extracted.add("emails")
        if phones:
            entity.contact.setdefault("phone_numbers", phones)
            evidence["phone_numbers"] = ", ".join(phones[:3])
            extracted.add("phone_numbers")
        if soup:
            for anchor in soup.find_all("a", href=True):
                href = urljoin(url or "", anchor["href"])
                label = self._clean(anchor.get_text(" ")).lower()
                domain = urlparse(href).netloc.lower()
                if "book" in label or "reserve" in label:
                    entity.booking_url = entity.booking_url or href
                if "menu" in label:
                    entity.menu_url = entity.menu_url or href
                if "ticket" in label or "register" in label:
                    entity.ticket_url = entity.ticket_url or href
                if "maps.google" in domain or "google.com/maps" in href:
                    entity.google_maps_url = entity.google_maps_url or href
                if "wa.me" in domain or "whatsapp" in domain:
                    entity.whatsapp_url = entity.whatsapp_url or href

    def _apply_location(
        self,
        entity: CityEntity,
        text: str,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        lower = text.lower()
        for locality in LOCALITIES:
            if locality in lower:
                entity.locality = entity.locality or locality.title()
                entity.neighborhood = entity.neighborhood or locality.title()
                entity.geo_precision = entity.geo_precision or ("exact" if entity.latitude else "locality")
                evidence["locality"] = locality
                extracted.add("locality")
                break
        pincode = re.search(r"\b(50[0-9]{4})\b", text)
        if pincode:
            entity.postal_code = entity.postal_code or pincode.group(1)
            extracted.add("postal_code")

    def _apply_timings(
        self,
        entity: CityEntity,
        text: str,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        lower = text.lower()
        if "24 hours" in lower or "open 24" in lower:
            entity.open_24_hours = True
            entity.opening_hours_raw = entity.opening_hours_raw or "24 hours"
            extracted.add("open_24_hours")
        time_line = re.search(
            r"((?:mon|tue|wed|thu|fri|sat|sun|daily|open|timings)[^.\n]{0,120}(?:am|pm|hours))",
            lower,
        )
        if time_line:
            entity.opening_hours_raw = entity.opening_hours_raw or time_line.group(1)
            entity.timings.setdefault("raw", entity.opening_hours_raw)
            evidence["opening_hours_raw"] = time_line.group(1)
            extracted.add("opening_hours_raw")
        if any(term in lower for term in ["midnight", "late night", "open late", "after midnight"]):
            entity.late_night = True
            entity.after_midnight = "midnight" in lower or "after midnight" in lower
            extracted.add("late_night")
        if "morning" in lower:
            entity.best_time_to_visit.append("morning")
        if "evening" in lower or "sunset" in lower:
            entity.best_time_to_visit.append("evening")
        if "quiet hours" in lower or "less crowded" in lower:
            entity.quiet_hours.append("mentioned as less crowded")

    def _apply_pricing(
        self,
        entity: CityEntity,
        text: str,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        lower = text.lower()
        prices = [int(value.replace(",", "")) for value in re.findall(r"(?:₹|rs\.?\s?)([0-9][0-9,]{1,6})", lower)]
        if prices:
            entity.pricing.setdefault("observed_prices_inr", prices[:10])
            entity.pricing.setdefault("ticket_price_min", min(prices))
            entity.pricing.setdefault("ticket_price_max", max(prices))
            entity.paid = True
            entity.price_level = entity.price_level or self._price_level(max(prices))
            evidence["pricing"] = ", ".join(f"₹{p}" for p in prices[:5])
            extracted.add("pricing")
        if any(term in lower for term in ["free entry", "free event", "entry free", "no entry fee"]):
            entity.free_entry = True
            entity.paid = False
            entity.price_level = entity.price_level or "free"
            extracted.add("free_entry")
        if "membership" in lower:
            entity.membership_required = True

    def _apply_rating(
        self,
        entity: CityEntity,
        text: str,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        match = re.search(r"\b([1-5](?:\.\d)?)\s*(?:/|out of)\s*5\b", text.lower())
        if match:
            entity.rating = entity.rating or float(match.group(1))
            evidence["rating"] = match.group(0)
            extracted.add("rating")
        count = re.search(r"\b([0-9][0-9,]{1,8})\s+(?:reviews|ratings)\b", text.lower())
        if count:
            entity.rating_count = entity.rating_count or int(count.group(1).replace(",", ""))
            entity.review_count = entity.review_count or entity.rating_count
            extracted.add("rating_count")

    def _apply_intents(
        self,
        entity: CityEntity,
        text: str,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        lower = text.lower()
        context_keys = self._matches(CONTEXT_RULES, lower)
        branch_ids = self._matches(BRANCH_RULES, lower)
        intent_tags = self._matches(INTENT_KEYWORDS, lower)
        negative_tags = self._matches(NEGATIVE_TAGS, lower)
        entity.metadata.context_keys = sorted(set(entity.metadata.context_keys + context_keys))
        entity.metadata.branch_ids = sorted(set(entity.metadata.branch_ids + branch_ids))
        entity.metadata.intent_tags = sorted(set(entity.metadata.intent_tags + intent_tags))
        entity.metadata.negative_intent_tags = sorted(set(entity.metadata.negative_intent_tags + negative_tags))
        for tag in intent_tags:
            entity.metadata.suitability_scores[tag] = max(
                entity.metadata.suitability_scores.get(tag, 0.0),
                self._score_keywords(INTENT_KEYWORDS[tag], lower),
            )
        if context_keys:
            evidence["context_keys"] = ", ".join(context_keys)
            extracted.add("context_keys")
        if branch_ids:
            evidence["branch_ids"] = ", ".join(branch_ids)
            extracted.add("branch_ids")
        if intent_tags:
            evidence["intent_tags"] = ", ".join(intent_tags)
            extracted.add("intent_tags")

    def _apply_atmosphere(self, entity: CityEntity, text: str) -> None:
        lower = text.lower()
        atmosphere = []
        for label in ["quiet", "peaceful", "lively", "romantic", "casual", "premium", "spiritual", "sporty", "intellectual", "artsy", "family"]:
            if label in lower:
                atmosphere.append(label)
        entity.metadata.atmosphere = sorted(set(entity.metadata.atmosphere + atmosphere))
        if any(term in lower for term in ["loud", "noisy", "dj"]):
            entity.metadata.noise_level = "loud"
        elif any(term in lower for term in ["quiet", "peaceful", "silent"]):
            entity.metadata.noise_level = "quiet"
        for crowd in ["students", "families", "founders", "investors", "tourists", "couples", "friends"]:
            if crowd.rstrip("s") in lower or crowd in lower:
                entity.metadata.crowd_type.append(crowd)
                entity.metadata.audience_type.append(crowd)
        entity.metadata.crowd_type = sorted(set(entity.metadata.crowd_type))
        entity.metadata.audience_type = sorted(set(entity.metadata.audience_type))
        entity.metadata.solo_friendly = self._bool_hint(lower, ["solo"], ["not solo"])
        entity.metadata.couple_friendly = self._bool_hint(lower, ["couple", "date"], ["not for couples"])
        entity.metadata.family_friendly = self._bool_hint(lower, ["family", "kids"], ["not for families"])
        entity.metadata.work_friendly = self._bool_hint(lower, ["wifi", "laptop", "work friendly"], ["not work friendly"])
        entity.metadata.women_friendly = self._bool_hint(lower, ["women friendly", "safe for women", "safe"], ["unsafe"])
        entity.metadata.first_timer_friendly = self._bool_hint(lower, ["first timer", "beginner friendly"], [])

    def _apply_amenities(self, entity: CityEntity, text: str) -> None:
        lower = text.lower()
        for amenity, terms in AMENITY_PATTERNS.items():
            value = any(term in lower for term in terms)
            entity.amenity_flags.setdefault(amenity, value if value else None)
            if value and amenity not in entity.amenities:
                entity.amenities.append(amenity)

    def _apply_media(self, entity: CityEntity, soup: BeautifulSoup | None, url: str | None) -> None:
        if not soup:
            return
        assets: list[MediaAsset] = []
        seen: set[str] = set()
        og_image = self._meta(soup, "og:image")
        if og_image:
            assets.append(self._media(og_image, url, is_primary=True))
            seen.add(assets[-1].url)
        for image in soup.find_all("img", src=True):
            src = urljoin(url or "", image["src"])
            if src in seen or not src.startswith(("http://", "https://")):
                continue
            alt = self._clean(image.get("alt"))
            asset = self._media(src, url, alt=alt, caption=alt)
            assets.append(asset)
            seen.add(src)
            if len(assets) >= 20:
                break
        entity.media = self._unique_media(entity.media + assets)

    def _apply_event_or_community(
        self,
        entity: CityEntity,
        text: str,
        url: str | None,
        evidence: dict[str, str],
        extracted: set[str],
    ) -> None:
        lower = text.lower()
        if any(term in lower for term in ["event", "workshop", "meetup", "concert", "ticket", "register"]):
            entity.entity_type = "event" if entity.category == "event" else entity.entity_type
            entity.event_details = entity.event_details or EventDetails()
            entity.event_details.event_name = entity.event_details.event_name or entity.name
            entity.event_details.ticket_url = entity.event_details.ticket_url or entity.ticket_url
            entity.event_details.event_topics = sorted(set(entity.metadata.intent_tags + entity.metadata.branch_ids))
            entity.event_details.event_audience = sorted(set(entity.metadata.audience_type))
            date_text = self._date_hint(text)
            if date_text:
                evidence["event_date_text"] = date_text
                extracted.add("event_date_text")
            if "cancelled" in lower:
                entity.event_details.event_status = "cancelled"
            elif "postponed" in lower:
                entity.event_details.event_status = "postponed"
            else:
                entity.event_details.event_status = "scheduled"
            extracted.add("event_details")
        if any(term in lower for term in ["community", "club", "group", "ngo", "volunteer", "meetup"]):
            entity.community_details = entity.community_details or CommunityDetails()
            entity.community_details.community_name = entity.community_details.community_name or entity.name
            entity.community_details.community_type = entity.community_details.community_type or self._community_type(lower)
            entity.community_details.online_presence = sorted(set(entity.social_links.values()))
            entity.community_details.community_topics = sorted(set(entity.metadata.intent_tags))
            entity.community_details.community_audience = sorted(set(entity.metadata.audience_type))
            member_match = re.search(r"\b([0-9][0-9,]{1,8})\s+(?:members|followers)\b", lower)
            if member_match:
                entity.community_details.member_count = int(member_match.group(1).replace(",", ""))
            extracted.add("community_details")

    def _review_signals(self, entity: CityEntity, text: str, url: str | None) -> list[TextSignal]:
        signals = []
        for idx, sentence in enumerate(self._sentences(text)):
            lower = sentence.lower()
            if not any(term in lower for term in ["review", "people say", "visitors", "users", "crowded", "quiet", "safe", "expensive", "good", "bad"]):
                continue
            signals.append(
                TextSignal(
                    id=self._hash(f"{entity.id}:{idx}:{sentence}")[:16],
                    source="web_page_extracted",
                    text=sentence[:1000],
                    url=url,
                    sentiment=self._sentiment(sentence),
                    sentiment_score=self._sentiment_score(sentence),
                    topics=self._keywords(sentence),
                    mentioned_intents=self._matches(INTENT_KEYWORDS, sentence.lower()),
                    mentioned_safety_signals=self._matches({"safety": ["safe", "unsafe", "well lit"]}, sentence.lower()),
                    mentioned_price_signals=self._matches({"price": ["free", "budget", "expensive", "₹", "rs"]}, sentence.lower()),
                    confidence=0.35,
                )
            )
            if len(signals) >= 12:
                break
        return signals

    def _relationships(self, entity: CityEntity, text: str, url: str | None) -> list[Relationship]:
        relationships = []
        for locality in LOCALITIES:
            if locality in text.lower() and entity.locality and locality != entity.locality.lower():
                relationships.append(
                    Relationship(
                        subject_id=entity.id,
                        predicate="near",
                        object_id=locality.replace(" ", "_"),
                        object_name=locality.title(),
                        confidence=0.35,
                        source="web_text",
                        evidence=f"mentions {locality}",
                        source_url=url,
                    )
                )
        for tag in entity.metadata.intent_tags[:8]:
            relationships.append(
                Relationship(
                    subject_id=entity.id,
                    predicate="recommended_for",
                    object_id=tag,
                    object_name=tag,
                    confidence=entity.metadata.suitability_scores.get(tag, 0.5),
                    source="intent_extraction",
                    evidence=f"matched intent tag {tag}",
                    source_url=url,
                )
            )
        return relationships

    def _dedupe(self, entity: CityEntity, text: str):
        dedupe = entity.dedupe
        dedupe.name_fingerprint = self._fingerprint(entity.name)
        dedupe.address_fingerprint = self._fingerprint(entity.address)
        phones = entity.contact.get("phone_numbers") or entity.contact.get("phone")
        dedupe.phone_fingerprint = self._fingerprint(" ".join(phones) if isinstance(phones, list) else phones)
        dedupe.website_fingerprint = self._fingerprint(str(entity.website) if entity.website else None)
        if entity.latitude is not None and entity.longitude is not None:
            dedupe.geo_hash = f"{entity.latitude:.4f},{entity.longitude:.4f}"
        dedupe.content_fingerprint = self._hash(text[:2000])
        dedupe.canonical_confidence = entity.confidence_score
        return dedupe

    def _card(self, entity: CityEntity) -> AppCard:
        image = next((asset.url for asset in entity.media if asset.is_primary), None)
        image = image or (entity.media[0].url if entity.media else None)
        mood_tags = sorted(set(entity.metadata.intent_tags + entity.metadata.atmosphere))[:8]
        why = []
        if entity.metadata.context_keys:
            why.append(f"Matches {', '.join(entity.metadata.context_keys[:2]).lower().replace('_', ' ')}")
        if entity.metadata.audience_type:
            why.append(f"Popular with {', '.join(entity.metadata.audience_type[:2])}")
        if entity.locality:
            why.append(f"Located in {entity.locality}")
        return AppCard(
            title=entity.name,
            subtitle=" · ".join(part for part in [entity.primary_category or entity.category, entity.locality] if part),
            description=entity.summary or entity.description,
            primary_image_url=image,
            address_short=entity.locality or entity.address,
            timing_label=entity.opening_hours_raw or ("Open 24 hours" if entity.open_24_hours else None),
            rating_label=f"{entity.rating} ({entity.rating_count})" if entity.rating else None,
            price_label=entity.price_level,
            mood_tags=mood_tags,
            why_match=why,
            cta_type="book" if entity.booking_url else "view",
            cta_url=entity.booking_url or entity.ticket_url or str(entity.website or ""),
        )

    def _audit(self, entity: CityEntity, extracted: set[str], evidence: dict[str, str]) -> ExtractionAudit:
        important = {
            "name",
            "category",
            "description",
            "locality",
            "address",
            "latitude",
            "longitude",
            "website",
            "context_keys",
            "intent_tags",
            "opening_hours_raw",
            "pricing",
            "media",
        }
        present = {field for field in important if getattr(entity, field, None)}
        fields = sorted(present | extracted)
        return ExtractionAudit(
            extractor_version=self.version,
            fields_extracted=fields,
            fields_missing=sorted(important - set(fields)),
            field_confidence={field: 0.75 for field in fields},
            evidence_map=evidence,
        )

    def _media(self, src: str, base_url: str | None, alt: str | None = None, caption: str | None = None, is_primary: bool = False) -> MediaAsset:
        url = urljoin(base_url or "", src)
        labels = self._keywords(" ".join([url, alt or "", caption or ""]))[:8]
        lower = " ".join(labels + [alt or "", caption or ""]).lower()
        return MediaAsset(
            id=self._hash(url)[:20],
            source="web_page",
            url=url,
            caption=caption,
            alt_text=alt,
            labels=labels,
            detected_objects=labels,
            people_visible=self._bool_hint(lower, ["people", "crowd", "person"], []),
            food_visible=self._bool_hint(lower, ["food", "coffee", "biryani", "restaurant"], []),
            interior_visible=self._bool_hint(lower, ["interior", "inside"], []),
            exterior_visible=self._bool_hint(lower, ["exterior", "outside"], []),
            night_view=self._bool_hint(lower, ["night", "midnight", "evening"], []),
            quality_score=0.5,
            is_primary=is_primary,
            copyright_risk="unknown",
        )

    def _candidate_names_from_text(self, text: str) -> list[str]:
        names: list[str] = []
        patterns = [
            r"(?:^|\n)\s*(?:\d{1,2}[.)-]\s+|[-*]\s+)([A-Z][A-Za-z0-9&'. -]{2,80})",
            r"\*\*([A-Z][A-Za-z0-9&'. -]{2,80})\*\*",
            r"\[([A-Z][A-Za-z0-9&'. -]{2,80})\]\((?:https?://)[^)]+\)",
            r"\b([A-Z][A-Za-z0-9&'. -]{2,70}\s(?:Lake|Park|Fort|Palace|Temple|Museum|Road|Garden|Mall|Market|Cafe|Café|Restaurant|Bar|Pub|Brewery|Workspace|Coworking|Stadium|Club|Foundation|Community|Meetup|Festival|Conference|Summit))\b",
        ]
        for pattern in patterns:
            names.extend(re.findall(pattern, text[:30000], flags=re.MULTILINE))
        return names

    def _candidate_names_from_soup(self, soup: BeautifulSoup) -> list[str]:
        names: list[str] = []
        for tag in soup.find_all(["h2", "h3", "h4", "li"]):
            value = self._clean(tag.get_text(" "))
            if len(value) <= 100:
                names.append(value)
        return names

    def _clean_entity_name(self, value: str) -> str:
        value = self._clean(value)
        value = re.sub(r"^\d{1,2}[.)-]\s*", "", value)
        value = re.split(r"\s+[-–—:]\s+", value, maxsplit=1)[0]
        value = re.sub(r"\s*[-|:]\s*(?:Hyderabad|Secunderabad|Telangana|Review|Timings|Entry Fee).*$", "", value, flags=re.I)
        value = re.sub(r"\s+\(?\d{4}\)?$", "", value)
        return value.strip(" -–—:|")

    def _valid_entity_name(self, value: str) -> bool:
        if not value or len(value) < 3 or len(value) > 80:
            return False
        lower = value.lower()
        if re.match(r"^(?:address|timings?|entry fee|cancellation policy|privacy policy|terms)", lower):
            return False
        if lower in BAD_ENTITY_NAME_TERMS:
            return False
        if any(term in lower for term in ["http", "www.", "click here", "read more", "view all"]):
            return False
        if re.search(r"\.(?:webp|jpg|jpeg|png|gif|svg)(?:\b|$)", lower):
            return False
        if any(term in lower for term in ["profile pic", "author profile", "slogan mobile", "tour packages", "upcoming trips", "group trips", "india trips", "international trips"]):
            return False
        if any(term in lower for term in ARTICLE_TITLE_TERMS):
            return False
        words = re.findall(r"[A-Za-z0-9]+", value)
        if len(words) > 8:
            return False
        generic_tokens = {"trip", "trips", "package", "packages", "tour", "tours", "policy", "overview", "history", "weather"}
        if set(word.lower() for word in words).intersection(generic_tokens) and not any(
            hint in lower for hint in ["fort", "temple", "palace", "lake", "park", "cafe", "restaurant", "bar", "bazaar", "museum"]
        ):
            return False
        if len(words) == 1 and lower not in {"charminar", "gachibowli"}:
            return False
        if not any(ch.isalpha() for ch in value):
            return False
        if value.islower():
            return False
        return True

    def _media_for_entity(self, name: str, soup: BeautifulSoup, source_url: str) -> list[MediaAsset]:
        name_tokens = {
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]+", name)
            if len(token) > 3
        }
        if not name_tokens:
            return []
        assets: list[MediaAsset] = []
        seen: set[str] = set()
        for image in soup.find_all("img", src=True):
            src = urljoin(source_url, image["src"])
            if src in seen or not src.startswith(("http://", "https://")):
                continue
            alt = self._clean(image.get("alt"))
            title = self._clean(image.get("title"))
            haystack = " ".join([alt, title, src]).lower()
            if not any(token in haystack for token in name_tokens):
                continue
            assets.append(self._media(src, source_url, alt=alt, caption=title or alt, is_primary=not assets))
            seen.add(src)
            if len(assets) >= 4:
                break
        return assets

    def _evidence_window(self, text: str, name: str) -> str:
        for line in text.splitlines():
            if re.search(re.escape(name), line, flags=re.I):
                clean_line = self._clean(line)
                if clean_line:
                    return clean_line[:1000]
        match = re.search(re.escape(name), text, flags=re.I)
        if not match:
            return ""
        start = max(0, match.start() - 350)
        end = min(len(text), match.end() + 650)
        return self._clean(text[start:end])

    def _category_from_name(self, name: str, evidence: str) -> str:
        lower = f"{name} {evidence}".lower()
        if any(term in lower for term in ["cafe", "café", "restaurant", "biryani", "bar", "pub", "brewery"]):
            return "restaurant"
        if any(term in lower for term in ["event", "festival", "concert", "workshop", "meetup", "conference", "summit"]):
            return "event"
        if any(term in lower for term in ["community", "club", "group", "ngo", "foundation", "collective"]):
            return "community"
        if any(term in lower for term in ["cowork", "workspace", "startup", "company", "store", "shop"]):
            return "business"
        if any(term in lower for term in ["badminton", "football", "gaming", "fitness", "gym", "yoga", "cricket"]):
            return "activity"
        if any(term in lower for term in ["lake", "park", "fort", "palace", "temple", "museum", "garden", "mall", "market", "road"]):
            return "attraction"
        return "place"

    def _entity_type(self, category: str) -> str:
        if category in {"event", "community"}:
            return category
        return "place"

    def _locality_from_text(self, text: str) -> str | None:
        lower = text.lower()
        for locality in LOCALITIES:
            if locality in lower:
                return locality.title()
        if "hyderabad" in lower:
            return "Hyderabad"
        return None

    def _matches(self, rules: dict[str, list[str]], text: str) -> list[str]:
        return [key for key, terms in rules.items() if any(self._has_term(text, term) for term in terms)]

    def _score_keywords(self, terms: Iterable[str], text: str) -> float:
        hits = sum(1 for term in terms if self._has_term(text, term))
        return round(min(1.0, 0.35 + hits * 0.15), 3)

    def _bool_hint(self, text: str, positive: list[str], negative: list[str]) -> bool | None:
        if any(term in text for term in negative):
            return False
        if any(term in text for term in positive):
            return True
        return None

    def _sentiment(self, text: str) -> str:
        score = self._sentiment_score(text)
        if score > 0.2:
            return "positive"
        if score < -0.2:
            return "negative"
        return "mixed"

    def _sentiment_score(self, text: str) -> float:
        lower = text.lower()
        positive = sum(lower.count(term) for term in ["good", "great", "best", "safe", "clean", "friendly", "beautiful", "amazing"])
        negative = sum(lower.count(term) for term in ["bad", "worst", "unsafe", "dirty", "expensive", "crowded", "rude"])
        total = positive + negative
        return 0.0 if total == 0 else round((positive - negative) / total, 3)

    def _community_type(self, lower: str) -> str:
        if "ngo" in lower or "volunteer" in lower:
            return "ngo"
        if "startup" in lower or "founder" in lower:
            return "startup_group"
        if "sports" in lower:
            return "sports_group"
        if "religious" in lower or "spiritual" in lower:
            return "religious_group"
        return "club"

    def _date_hint(self, text: str) -> str | None:
        patterns = [
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}(?:,\s*\d{4})?\b",
            r"\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*(?:\s+\d{4})?\b",
            r"\b(?:today|tomorrow|this weekend|every sunday|every saturday)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text.lower())
            if match:
                return match.group(0)
        return None

    def _price_level(self, amount: int) -> str:
        if amount == 0:
            return "free"
        if amount <= 300:
            return "budget"
        if amount <= 1200:
            return "mid"
        if amount <= 3000:
            return "premium"
        return "luxury"

    def _confidence(self, entity: CityEntity, text: str) -> float:
        score = 0.2
        for value in [entity.name, entity.category, entity.description, entity.locality, entity.address, entity.website]:
            if value:
                score += 0.08
        if entity.latitude is not None and entity.longitude is not None:
            score += 0.15
        if len(text) > 500:
            score += 0.1
        return round(min(1.0, score), 3)

    def _unique_media(self, assets: list[MediaAsset]) -> list[MediaAsset]:
        seen = set()
        out = []
        for asset in assets:
            if asset.url not in seen:
                out.append(asset)
                seen.add(asset.url)
        return out

    def _unique_relationships(self, relationships: list[Relationship]) -> list[Relationship]:
        seen = set()
        out = []
        for rel in relationships:
            key = (rel.subject_id, rel.predicate, rel.object_id)
            if key not in seen:
                out.append(rel)
                seen.add(key)
        return out

    def _keywords(self, text: str) -> list[str]:
        stop = {"with", "from", "that", "this", "https", "http", "html", "image", "photo", "and", "the"}
        words = [word for word in re.findall(r"[a-z][a-z0-9_-]{2,}", text.lower()) if word not in stop]
        out = []
        for word in words:
            if word not in out:
                out.append(word)
            if len(out) >= 16:
                break
        return out

    def _sentences(self, text: str) -> list[str]:
        return [self._clean(item) for item in re.split(r"(?<=[.!?])\s+", text) if len(item.strip()) > 40]

    def _sentence(self, text: str) -> str | None:
        sentences = self._sentences(text)
        return sentences[0][:500] if sentences else (text[:500] if text else None)

    def _title(self, soup: BeautifulSoup | None) -> str | None:
        if not soup:
            return None
        if soup.title and soup.title.string:
            return self._clean(soup.title.string)
        h1 = soup.find("h1")
        return self._clean(h1.get_text(" ")) if h1 else None

    def _headings(self, soup: BeautifulSoup | None) -> list[str]:
        if not soup:
            return []
        return [self._clean(tag.get_text(" ")) for tag in soup.find_all(["h1", "h2", "h3"])[:30]]

    def _meta(self, soup: BeautifulSoup | None, name: str) -> str | None:
        if not soup:
            return None
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        content = tag.get("content") if tag else None
        return self._clean(content) if isinstance(content, str) else None

    def _has_term(self, text: str, term: str) -> bool:
        if not term:
            return False
        pattern = rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])"
        return re.search(pattern, text.lower()) is not None

    def _fingerprint(self, value: str | None) -> str | None:
        if not value:
            return None
        return re.sub(r"[^a-z0-9]+", "", value.lower()) or None

    def _hash(self, value: str) -> str:
        return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()

    def _clean(self, value: str | None) -> str:
        return re.sub(r"\s+", " ", value or "").strip()
