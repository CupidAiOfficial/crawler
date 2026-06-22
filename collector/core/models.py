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
    text: str
    url: str | None = None
    created_at: datetime | None = None
    fetched_at: datetime = Field(default_factory=utc_now)
    sentiment: str | None = None
    topics: list[str] = Field(default_factory=list)
    mentioned_entities: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class MediaAsset(BaseModel):
    id: str
    source: str
    url: str
    local_path: str | None = None
    kind: str = "image"
    license: str | None = None
    labels: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class Relationship(BaseModel):
    subject_id: str
    predicate: str
    object_id: str
    confidence: float = 0.5
    source: str
    metadata: dict[str, Any] = Field(default_factory=dict)


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
    source_counts: dict[str, int] = Field(default_factory=dict)
    updated_at: datetime = Field(default_factory=utc_now)


class CityEntity(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    category: str
    subcategories: list[str] = Field(default_factory=list)
    description: str | None = None
    locality: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timings: dict[str, Any] = Field(default_factory=dict)
    contact: dict[str, Any] = Field(default_factory=dict)
    website: HttpUrl | str | None = None
    social_links: dict[str, str] = Field(default_factory=dict)
    ratings: list[Rating] = Field(default_factory=list)
    amenities: list[str] = Field(default_factory=list)
    pricing: dict[str, Any] = Field(default_factory=dict)
    audience: list[str] = Field(default_factory=list)
    popularity: dict[str, Any] = Field(default_factory=dict)
    related_entities: list[str] = Field(default_factory=list)
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
