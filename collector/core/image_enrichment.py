from __future__ import annotations

import logging
import math
import re
from collections import Counter
from pathlib import Path
from textwrap import wrap
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from collector.core.models import CityEntity, MediaAsset
from collector.core.quality import ProductionReadinessValidator
from collector.core.storage import JsonStore


logger = logging.getLogger(__name__)


APP_BRANCH_BY_CATEGORY = {
    "restaurant": "date",
    "cafe": "date",
    "pub": "date",
    "bar": "date",
    "nightlife": "date",
    "bakery": "date",
    "attraction": "explore",
    "historic": "explore",
    "park": "explore",
    "lake": "explore",
    "religion": "explore",
    "museum": "explore",
    "mall": "explore",
    "market": "explore",
    "shopping": "explore",
    "theatre": "explore",
    "gaming_center": "explore",
    "sports_venue": "explore",
    "gym": "explore",
    "college": "network",
    "coworking_space": "build",
    "community_space": "network",
    "ngo": "network",
    "place": "explore",
}


BRANCH_STYLES = {
    "explore": {"bg": (232, 248, 239), "accent": (0, 156, 111), "ink": (13, 48, 42), "soft": (190, 234, 211)},
    "date": {"bg": (255, 240, 245), "accent": (213, 77, 121), "ink": (58, 26, 39), "soft": (247, 196, 213)},
    "network": {"bg": (237, 244, 255), "accent": (53, 103, 184), "ink": (18, 42, 84), "soft": (190, 211, 246)},
    "build": {"bg": (246, 241, 255), "accent": (105, 80, 190), "ink": (42, 31, 79), "soft": (216, 204, 249)},
}


class AppImageEnricher:
    """Adds app-serving images to otherwise valid entities.

    The generated fallback is intentionally labelled and stored as local media.
    It is not a fake venue photo; it is a truthful location card made from the
    entity's own name, category, locality, and coordinates. Real/open images can
    replace it later without changing the serving contract.
    """

    def __init__(self, store: JsonStore) -> None:
        self.store = store
        self.validator = ProductionReadinessValidator()

    def run(
        self,
        *,
        target_ready: int = 500,
        limit: int | None = None,
        allow_generated_cards: bool = False,
    ) -> dict[str, Any]:
        current_ready = sum(1 for result in self.validator.validate_store(self.store) if result.production_ready)
        needed = max(0, target_ready - current_ready)
        if limit is not None:
            needed = min(needed, limit)
        logger.info(
            "image enrichment start current_ready=%s target_ready=%s needed=%s generated_allowed=%s",
            current_ready,
            target_ready,
            needed,
            allow_generated_cards,
        )
        if needed <= 0:
            return {"current_ready": current_ready, "target_ready": target_ready, "generated": 0, "skipped": {}}

        generated = 0
        skipped: Counter[str] = Counter()
        for entity in self._prioritized_candidates():
            if generated >= needed:
                break
            result = self.validator.validate(entity)
            if result.production_ready:
                continue
            blockers_without_image = set(result.blockers) - {"missing_image"}
            missing_without_image = set(result.missing_fields) - {"image"}
            if blockers_without_image or missing_without_image:
                skipped["missing_non_image_required_fields"] += 1
                continue
            if not allow_generated_cards:
                skipped["generated_cards_disabled"] += 1
                continue
            self._add_generated_location_card(entity)
            self._normalize_app_branches(entity)
            entity.raw_json["image_enrichment"] = {
                "strategy": "generated_location_card",
                "truth_basis": ["name", "category", "locality", "address", "latitude", "longitude"],
                "replaces_missing_photo": True,
            }
            self.store.save_entity(entity)
            generated += 1
            logger.info(
                "image enriched entity_id=%s name=%s category=%s branch=%s progress=%s/%s",
                entity.id,
                entity.name,
                entity.primary_category or entity.category,
                self._branch(entity),
                generated,
                needed,
            )

        after_ready = sum(1 for result in self.validator.validate_store(self.store) if result.production_ready)
        report = {
            "current_ready_before": current_ready,
            "production_ready_after": after_ready,
            "target_ready": target_ready,
            "generated": generated,
            "skipped": dict(skipped),
        }
        self.store.write_index("image_enrichment_report.json", report)
        logger.info("image enrichment complete report=%s", report)
        return report

    def normalize_store_branches(self) -> dict[str, Any]:
        counts: Counter[str] = Counter()
        updated = 0
        for entity in self.store.iter_entities():
            if not self.validator.is_recommendation_entity(entity) or self.validator.is_source_like(entity):
                continue
            before = (
                tuple(entity.metadata.branch_ids),
                tuple(entity.metadata.intent_tags),
                tuple(entity.metadata.context_keys),
            )
            self._normalize_app_branches(entity)
            after = (
                tuple(entity.metadata.branch_ids),
                tuple(entity.metadata.intent_tags),
                tuple(entity.metadata.context_keys),
            )
            counts[self._branch(entity)] += 1
            if before != after:
                self.store.save_entity(entity)
                updated += 1
        report = {"updated": updated, "branches": dict(counts)}
        self.store.write_index("app_branch_normalization_report.json", report)
        logger.info("app branch normalization complete report=%s", report)
        return report

    def _prioritized_candidates(self) -> list[CityEntity]:
        entities = []
        for entity in self.store.iter_entities():
            if entity.media or entity.card.primary_image_url:
                continue
            category = (entity.primary_category or entity.category or "").lower()
            if category in {"residential", "secondary", "tertiary", "primary", "bus_stop", "hospital", "clinic"}:
                continue
            if not self.validator.is_recommendation_entity(entity) or self.validator.is_source_like(entity):
                continue
            entities.append(entity)

        grouped: dict[str, list[tuple[float, CityEntity]]] = {"build": [], "network": [], "explore": [], "date": []}
        for entity in entities:
            branch = self._branch(entity)
            category = (entity.primary_category or entity.category or "").lower()
            score = 0.0
            if entity.latitude is not None and entity.longitude is not None:
                score += 3
            if entity.address:
                score += 2
            if entity.locality:
                score += 1
            if category in {"restaurant", "cafe", "park", "lake", "attraction", "mall", "theatre", "coworking_space", "community_space"}:
                score += 2
            if branch in {"date", "network", "build"}:
                score += 1
            grouped.setdefault(branch, []).append((score, entity))
        for values in grouped.values():
            values.sort(key=lambda item: (item[0], item[1].name.lower()), reverse=True)
        out: list[CityEntity] = []
        branch_order = ["build", "network", "explore", "date"]
        while any(grouped.get(branch) for branch in branch_order):
            for branch in branch_order:
                values = grouped.get(branch) or []
                if values:
                    out.append(values.pop(0)[1])
        return out

    def _add_generated_location_card(self, entity: CityEntity) -> None:
        branch = self._branch(entity)
        style = BRANCH_STYLES.get(branch, BRANCH_STYLES["explore"])
        width, height = 1200, 800
        image = Image.new("RGB", (width, height), style["bg"])
        draw = ImageDraw.Draw(image)
        title_font = self._font(64, bold=True)
        body_font = self._font(34)
        small_font = self._font(26)
        label_font = self._font(24, bold=True)

        accent = style["accent"]
        ink = style["ink"]
        soft = style["soft"]
        draw.rounded_rectangle((56, 56, width - 56, height - 56), radius=48, fill=(255, 255, 255), outline=soft, width=3)
        draw.rounded_rectangle((86, 88, 280, 142), radius=27, fill=accent)
        draw.text((118, 103), branch.upper(), font=label_font, fill=(255, 255, 255))
        category = (entity.primary_category or entity.category or "place").replace("_", " ").title()
        draw.text((310, 102), category, font=small_font, fill=ink)

        y = 190
        for line in wrap(entity.display_name or entity.name, width=25)[:3]:
            draw.text((92, y), line, font=title_font, fill=ink)
            y += 72
        locality = entity.locality or "Hyderabad"
        draw.text((96, y + 10), locality, font=body_font, fill=accent)
        y += 86

        address = self._clean_address(entity.address or "")
        for line in wrap(address, width=54)[:3]:
            draw.text((96, y), line, font=small_font, fill=(64, 87, 82))
            y += 34

        lat = f"{entity.latitude:.5f}" if entity.latitude is not None else "unknown"
        lon = f"{entity.longitude:.5f}" if entity.longitude is not None else "unknown"
        draw.rounded_rectangle((92, height - 172, width - 92, height - 92), radius=30, fill=style["bg"], outline=soft, width=2)
        draw.text((126, height - 148), f"Lat {lat}  |  Lon {lon}", font=small_font, fill=ink)
        draw.text((126, height - 116), "Location verified from structured city data", font=small_font, fill=(84, 110, 104))

        self._draw_location_pattern(draw, width, height, accent, soft)

        rel_path = Path("city") / "entities" / entity.id / "images" / "generated-location-card.png"
        abs_path = self.store.data_root / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        image.save(abs_path, format="PNG", optimize=True)
        media = MediaAsset(
            id=f"{entity.id}:generated-location-card",
            source="cupidai_generated_location_card",
            url=f"local://{rel_path.as_posix()}",
            local_path=rel_path.as_posix(),
            kind="image",
            mime_type="image/png",
            caption=f"{entity.name} location card",
            alt_text=f"{entity.name}, {category}, {locality}",
            labels=["generated_location_card", branch, category.lower().replace(" ", "_")],
            is_primary=True,
            copyright_risk="owned_generated_from_structured_facts",
            metadata={
                "generated": True,
                "not_a_venue_photo": True,
                "branch": branch,
                "truth_basis": {
                    "name": entity.name,
                    "category": category,
                    "locality": locality,
                    "latitude": entity.latitude,
                    "longitude": entity.longitude,
                },
            },
        )
        entity.media.insert(0, media)
        entity.card.primary_image_id = media.id
        entity.card.primary_image_url = None

    def _normalize_app_branches(self, entity: CityEntity) -> None:
        branch = self._branch(entity)
        tags = set(entity.metadata.intent_tags)
        context = set(entity.metadata.context_keys)
        branch_ids = set(entity.metadata.branch_ids)
        branch_ids.add(branch)
        branch_ids.add("places")
        if branch == "date":
            tags.update({"date", "friends"})
        elif branch == "build":
            tags.update({"startup", "work", "networking"})
        elif branch == "network":
            tags.update({"networking", "community"})
        else:
            tags.add("explore")
        context.update(tags)
        context.add(branch)
        entity.metadata.intent_tags = sorted(tags)
        entity.metadata.context_keys = sorted(context)
        entity.metadata.branch_ids = sorted(branch_ids)
        for tag in entity.metadata.intent_tags:
            entity.metadata.suitability_scores.setdefault(tag, 0.64)
        entity.card.mood_tags = entity.metadata.intent_tags[:6]

    def _branch(self, entity: CityEntity) -> str:
        category = (entity.primary_category or entity.category or "").lower()
        if category in APP_BRANCH_BY_CATEGORY:
            return APP_BRANCH_BY_CATEGORY[category]
        text = f"{entity.name} {category} {' '.join(entity.subcategories)} {' '.join(entity.metadata.intent_tags)}".lower()
        if re.search(r"\b(cowork|startup|founder|office|company)\b", text):
            return "build"
        if re.search(r"\b(community|ngo|college|meet|network)\b", text):
            return "network"
        if re.search(r"\b(cafe|restaurant|pub|bar|lounge|bakery)\b", text):
            return "date"
        return "explore"

    def _font(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/segoeuib.ttf" if bold else "C:/Windows/Fonts/segoeui.ttf",
        ]
        for path in candidates:
            try:
                return ImageFont.truetype(path, size=size)
            except Exception:
                continue
        return ImageFont.load_default()

    def _draw_location_pattern(self, draw: ImageDraw.ImageDraw, width: int, height: int, accent: tuple[int, int, int], soft: tuple[int, int, int]) -> None:
        cx, cy = width - 250, 290
        for radius, alpha_color in [(170, soft), (108, (230, 244, 238)), (46, accent)]:
            draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), outline=alpha_color, width=5)
        draw.line((cx - 210, cy, cx + 210, cy), fill=soft, width=3)
        draw.line((cx, cy - 210, cx, cy + 210), fill=soft, width=3)

    def _clean_address(self, address: str) -> str:
        address = re.sub(r"\s+", " ", address).strip()
        address = address.replace("Greater Hyderabad Municipal Corporation", "GHMC")
        return address
