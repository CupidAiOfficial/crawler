from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, HttpUrl


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CandidateKind(str, Enum):
    ENTITY = "entity"
    SOURCE_URL = "source_url"
    QUERY = "query"
    RELATIONSHIP = "relationship"


class SourcePolicy(str, Enum):
    PUBLIC_API = "public_api"
    OPEN_DATA = "open_data"
    HTML_ALLOWED = "html_allowed"
    OFFICIAL_API_REQUIRED = "official_api_required"
    MANUAL_IMPORT_ONLY = "manual_import_only"
    DISABLED = "disabled"


class CrawlCandidate(BaseModel):
    kind: CandidateKind
    value: str
    source: str
    priority: float = 1.0
    depth: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)
    discovered_at: datetime = Field(default_factory=utc_now)


class SourceRecord(BaseModel):
    source: str
    url: str | None = None
    source_id: str | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    license: str | None = None
    raw_path: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    canonical_url: str | None = None
    search_query: str | None = None
    search_rank: int | None = None
    discovered_from_url: str | None = None
    discovered_from_entity_id: str | None = None
    language: str | None = None
    content_hash: str | None = None
    crawl_status: str | None = None
    http_status: int | None = None
    extraction_confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Rating(BaseModel):
    source: str
    score: float | None = None
    count: int | None = None
    scale: float | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class TextSignal(BaseModel):
    id: str
    source: str
    author: str | None = None
    author_hash: str | None = None
    text: str
    url: str | None = None
    created_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    rating: float | None = None
    language: str | None = None
    sentiment: str | None = None
    sentiment_score: float | None = None
    topics: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    mentioned_entities: list[str] = Field(default_factory=list)
    mentioned_intents: list[str] = Field(default_factory=list)
    mentioned_time_context: list[str] = Field(default_factory=list)
    mentioned_crowd_type: list[str] = Field(default_factory=list)
    mentioned_safety_signals: list[str] = Field(default_factory=list)
    mentioned_price_signals: list[str] = Field(default_factory=list)
    confidence: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaAsset(BaseModel):
    id: str
    source: str
    url: str
    local_path: str | None = None
    kind: str = "image"
    mime_type: str | None = None
    byte_size: int | None = None
    content_hash: str | None = None
    thumbnail_url: str | None = None
    caption: str | None = None
    alt_text: str | None = None
    license: str | None = None
    labels: list[str] = Field(default_factory=list)
    detected_scene: str | None = None
    detected_objects: list[str] = Field(default_factory=list)
    people_visible: bool | None = None
    food_visible: bool | None = None
    interior_visible: bool | None = None
    exterior_visible: bool | None = None
    night_view: bool | None = None
    quality_score: float | None = None
    is_primary: bool = False
    copyright_risk: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relationship(BaseModel):
    subject_id: str
    predicate: str
    object_id: str
    object_name: str | None = None
    confidence: float = 0.5
    source: str
    evidence: str | None = None
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventDetails(BaseModel):
    event_name: str | None = None
    event_type: str | None = None
    event_start_at: datetime | None = None
    event_end_at: datetime | None = None
    event_timezone: str = "Asia/Kolkata"
    event_frequency: str | None = None
    organizer_name: str | None = None
    organizer_url: str | None = None
    venue_name: str | None = None
    venue_entity_id: str | None = None
    ticket_url: str | None = None
    registration_required: bool | None = None
    capacity: int | None = None
    age_restriction: str | None = None
    performers: list[str] = Field(default_factory=list)
    speakers: list[str] = Field(default_factory=list)
    event_topics: list[str] = Field(default_factory=list)
    event_audience: list[str] = Field(default_factory=list)
    event_status: str | None = None


class CommunityDetails(BaseModel):
    community_name: str | None = None
    community_type: str | None = None
    member_count: int | None = None
    activity_frequency: str | None = None
    meeting_location: str | None = None
    online_presence: list[str] = Field(default_factory=list)
    joining_method: str | None = None
    joining_fee: float | None = None
    moderation_status: str | None = None
    community_topics: list[str] = Field(default_factory=list)
    community_audience: list[str] = Field(default_factory=list)
    active_recently: bool | None = None


class DedupeSignals(BaseModel):
    name_fingerprint: str | None = None
    address_fingerprint: str | None = None
    phone_fingerprint: str | None = None
    website_fingerprint: str | None = None
    geo_hash: str | None = None
    content_fingerprint: str | None = None
    image_hashes: list[str] = Field(default_factory=list)
    possible_duplicate_ids: list[str] = Field(default_factory=list)
    canonical_confidence: float | None = None


class AppCard(BaseModel):
    title: str | None = None
    subtitle: str | None = None
    description: str | None = None
    primary_image_id: str | None = None
    primary_image_url: str | None = None
    address_short: str | None = None
    timing_label: str | None = None
    rating_label: str | None = None
    price_label: str | None = None
    mood_tags: list[str] = Field(default_factory=list)
    why_match: list[str] = Field(default_factory=list)
    cta_type: str | None = None
    cta_url: str | None = None


class ExtractionAudit(BaseModel):
    extracted_at: datetime = Field(default_factory=utc_now)
    extractor_version: str = "structured-extractor-v1"
    model_used: str | None = None
    fields_extracted: list[str] = Field(default_factory=list)
    fields_missing: list[str] = Field(default_factory=list)
    field_confidence: dict[str, float] = Field(default_factory=dict)
    evidence_map: dict[str, str] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EntityMetadata(BaseModel):
    sentiment: str | None = None
    topics: list[str] = Field(default_factory=list)
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    crowd_type: list[str] = Field(default_factory=list)
    atmosphere: list[str] = Field(default_factory=list)
    popularity_score: float | None = None
    audience_type: list[str] = Field(default_factory=list)
    suitability_scores: dict[str, float] = Field(default_factory=dict)
    hidden_gem_score: float | None = None
    intent_tags: list[str] = Field(default_factory=list)
    negative_intent_tags: list[str] = Field(default_factory=list)
    context_keys: list[str] = Field(default_factory=list)
    branch_ids: list[str] = Field(default_factory=list)
    vibe: str | None = None
    noise_level: str | None = None
    age_group_fit: list[str] = Field(default_factory=list)
    solo_friendly: bool | None = None
    couple_friendly: bool | None = None
    family_friendly: bool | None = None
    group_friendly: bool | None = None
    student_friendly: bool | None = None
    work_friendly: bool | None = None
    first_timer_friendly: bool | None = None
    women_friendly: bool | None = None
    lgbtq_friendly: bool | None = None
    accessibility_friendly: bool | None = None
    safety_signals: list[str] = Field(default_factory=list)
    trust_signals: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    content_quality_score: float | None = None
    source_quality_score: float | None = None
    freshness_score: float | None = None
    data_completeness_score: float | None = None
    needs_human_review: bool = False
    review_reason: str | None = None
    source_counts: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class CityEntity(BaseModel):
    id: str
    name: str
    display_name: str | None = None
    aliases: list[str] = Field(default_factory=list)
    entity_type: str = "place"
    category: str
    primary_category: str | None = None
    subcategories: list[str] = Field(default_factory=list)
    source_categories: list[str] = Field(default_factory=list)
    brand_or_chain_name: str | None = None
    is_chain: bool | None = None
    duplicate_candidates: list[str] = Field(default_factory=list)
    canonical_entity_id: str | None = None
    description: str | None = None
    summary: str | None = None
    locality: str | None = None
    sub_locality: str | None = None
    neighborhood: str | None = None
    city: str = "Hyderabad"
    district: str | None = None
    state: str = "Telangana"
    country: str = "India"
    address: str | None = None
    landmark_hint: str | None = None
    postal_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    geo_precision: str | None = None
    plus_code: str | None = None
    map_url: str | None = None
    nearby_metro: str | None = None
    nearby_bus_stop: str | None = None
    timings: dict[str, Any] = Field(default_factory=dict)
    opening_hours_raw: str | None = None
    opening_hours_structured: dict[str, Any] = Field(default_factory=dict)
    open_now: bool | None = None
    open_24_hours: bool = False
    late_night: bool = False
    after_midnight: bool = False
    best_time_to_visit: list[str] = Field(default_factory=list)
    peak_hours: list[str] = Field(default_factory=list)
    quiet_hours: list[str] = Field(default_factory=list)
    seasonal: bool = False
    temporarily_closed: bool = False
    permanently_closed: bool = False
    last_verified_at: datetime | None = None
    contact: dict[str, Any] = Field(default_factory=dict)
    website: HttpUrl | str | None = None
    booking_url: str | None = None
    menu_url: str | None = None
    ticket_url: str | None = None
    google_maps_url: str | None = None
    osm_url: str | None = None
    wikidata_url: str | None = None
    wikipedia_url: str | None = None
    whatsapp_url: str | None = None
    social_links: dict[str, str] = Field(default_factory=dict)
    ratings: list[Rating] = Field(default_factory=list)
    rating: float | None = None
    rating_count: int | None = None
    review_count: int | None = None
    source_rating_breakdown: dict[str, Any] = Field(default_factory=dict)
    trend_score: float | None = None
    social_mentions_count: int | None = None
    recent_mentions_count: int | None = None
    review_velocity: float | None = None
    last_review_at: datetime | None = None
    positive_review_ratio: float | None = None
    negative_review_ratio: float | None = None
    page_title: str | None = None
    meta_description: str | None = None
    headings: list[str] = Field(default_factory=list)
    full_text_excerpt: str | None = None
    quoted_evidence: list[str] = Field(default_factory=list)
    mentioned_entities: list[str] = Field(default_factory=list)
    mentioned_localities: list[str] = Field(default_factory=list)
    mentioned_activities: list[str] = Field(default_factory=list)
    mentioned_events: list[str] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    amenity_flags: dict[str, bool | None] = Field(default_factory=dict)
    pricing: dict[str, Any] = Field(default_factory=dict)
    price_level: str | None = None
    free_entry: bool | None = None
    paid: bool | None = None
    membership_required: bool | None = None
    audience: list[str] = Field(default_factory=list)
    popularity: dict[str, Any] = Field(default_factory=dict)
    related_entities: list[str] = Field(default_factory=list)
    media: list[MediaAsset] = Field(default_factory=list)
    event_details: EventDetails | None = None
    community_details: CommunityDetails | None = None
    relationships: list[Relationship] = Field(default_factory=list)
    dedupe: DedupeSignals = Field(default_factory=DedupeSignals)
    card: AppCard = Field(default_factory=AppCard)
    extraction_audit: ExtractionAudit = Field(default_factory=ExtractionAudit)
    raw_json: dict[str, Any] = Field(default_factory=dict)
    status: str = "ACTIVE"
    confidence_score: float | None = None
    source_count: int | None = None
    sources: list[SourceRecord] = Field(default_factory=list)
    metadata: EntityMetadata = Field(default_factory=EntityMetadata)
    first_seen_at: datetime = Field(default_factory=utc_now)
    last_seen_at: datetime = Field(default_factory=utc_now)


class CoverageSnapshot(BaseModel):
    generated_at: datetime = Field(default_factory=utc_now)
    city: str = "Hyderabad"
    entities_total: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    by_locality: dict[str, int] = Field(default_factory=dict)
    by_source: dict[str, int] = Field(default_factory=dict)
    crawl_frontier_size: int = 0
    plateau_signal: float = 0.0
