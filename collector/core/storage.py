from __future__ import annotations

import json
import logging
import hashlib
import threading
import time
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel

from collector.core.models import CityEntity, CrawlCandidate, EntityMetadata, MediaAsset, Relationship, SourceRecord, TextSignal


logger = logging.getLogger(__name__)


class JsonStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.city_root = data_root / "city"
        self.entities_root = self.city_root / "entities"
        self.indexes_root = self.city_root / "indexes"
        self.raw_root = data_root / "raw"
        self.checkpoint_root = data_root / "checkpoints"
        self._lock = threading.RLock()
        for path in [
            self.entities_root,
            self.indexes_root,
            self.raw_root,
            self.checkpoint_root,
        ]:
            path.mkdir(parents=True, exist_ok=True)

    def entity_dir(self, entity_id: str) -> Path:
        path = self.entities_root / entity_id
        (path / "images").mkdir(parents=True, exist_ok=True)
        (path / "videos").mkdir(parents=True, exist_ok=True)
        return path

    def save_entity(self, entity: CityEntity) -> None:
        folder = self.entity_dir(entity.id)
        logger.info("saving entity id=%s name=%s category=%s", entity.id, entity.name, entity.category)
        self._write_model(folder / "entity.json", entity)
        self._append_unique_models(folder / "source_snapshots.json", entity.sources)
        if entity.media:
            self.append_media(entity.id, entity.media)
        if entity.relationships:
            self.append_relationships(entity.id, entity.relationships)
        self._write_json(folder / "intents.json", {
            "context_keys": entity.metadata.context_keys,
            "branch_ids": entity.metadata.branch_ids,
            "intent_tags": entity.metadata.intent_tags,
            "negative_intent_tags": entity.metadata.negative_intent_tags,
            "suitability_scores": entity.metadata.suitability_scores,
        })
        self._write_model(folder / "metadata.json", entity.metadata)

    def load_entity(self, entity_id: str) -> CityEntity | None:
        path = self.entities_root / entity_id / "entity.json"
        if not path.exists():
            return None
        return CityEntity.model_validate_json(path.read_text(encoding="utf-8"))

    def iter_entities(self) -> Iterable[CityEntity]:
        for path in sorted(self.entities_root.glob("*/entity.json")):
            try:
                yield CityEntity.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning("skipping invalid entity file path=%s error=%s", path, exc)

    def append_reviews(self, entity_id: str, reviews: list[TextSignal]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "reviews.json", reviews)

    def append_comments(self, entity_id: str, comments: list[TextSignal]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "comments.json", comments)

    def append_relationships(self, entity_id: str, relationships: list[Relationship]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "relationships.json", relationships)

    def append_media(self, entity_id: str, media: list[MediaAsset]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "media.json", media)

    def append_sources(self, entity_id: str, sources: list[SourceRecord]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "source_snapshots.json", sources)

    def save_source_page(self, source: str, key: str, payload: dict[str, object]) -> str:
        folder = self.city_root / "source_pages"
        folder.mkdir(parents=True, exist_ok=True)
        safe_key = self._safe_key(key)
        path = folder / f"{safe_key}.json"
        self._write_json(path, payload)
        self._append_unique_dicts(self.indexes_root / "source_pages.json", [payload | {"path": str(path.relative_to(self.data_root))}])
        logger.info("saved source page source=%s url=%s", source, payload.get("url"))
        return str(path.relative_to(self.data_root))

    def iter_source_pages(self) -> Iterable[dict[str, object]]:
        folder = self.city_root / "source_pages"
        if not folder.exists():
            return
        for path in sorted(folder.glob("*.json")):
            payload = self._read_json(path, {})
            if isinstance(payload, dict):
                yield payload

    def save_metadata(self, entity_id: str, metadata: EntityMetadata) -> None:
        self._write_model(self.entity_dir(entity_id) / "metadata.json", metadata)

    def save_frontier(self, candidates: list[CrawlCandidate]) -> None:
        logger.info("saving frontier size=%s", len(candidates))
        self._write_json(
            self.checkpoint_root / "frontier.json",
            [candidate.model_dump(mode="json") for candidate in candidates],
        )

    def append_failed_candidate(self, candidate: CrawlCandidate) -> None:
        path = self.checkpoint_root / "failed_candidates.json"
        existing = self._read_json(path, [])
        payload = candidate.model_dump(mode="json")
        key = json.dumps(
            {
                "kind": payload.get("kind"),
                "source": payload.get("source"),
                "value": str(payload.get("value", "")).lower(),
                "error": payload.get("metadata", {}).get("last_error"),
            },
            sort_keys=True,
        )
        seen = {
            json.dumps(
                {
                    "kind": item.get("kind"),
                    "source": item.get("source"),
                    "value": str(item.get("value", "")).lower(),
                    "error": item.get("metadata", {}).get("last_error"),
                },
                sort_keys=True,
            )
            for item in existing
            if isinstance(item, dict)
        }
        if key not in seen:
            existing.append(payload)
            self._write_json(path, existing)
        logger.info("recorded failed candidate source=%s kind=%s value=%s", candidate.source, candidate.kind, candidate.value)

    def load_frontier(self) -> list[CrawlCandidate]:
        path = self.checkpoint_root / "frontier.json"
        if not path.exists():
            return []
        candidates: list[CrawlCandidate] = []
        payload = self._read_json(path, [])
        if not isinstance(payload, list):
            logger.warning("invalid frontier payload path=%s expected=list", path)
            return []
        for item in payload:
            try:
                candidates.append(CrawlCandidate.model_validate(item))
            except Exception as exc:
                logger.warning("skipping invalid frontier candidate path=%s error=%s payload=%s", path, exc, item)
        return candidates

    def save_raw(self, source: str, key: str, payload: object) -> str:
        folder = self.raw_root / source
        folder.mkdir(parents=True, exist_ok=True)
        safe_key = self._safe_key(key)
        path = folder / f"{safe_key}.json"
        self._write_json(path, payload)
        logger.debug("saved raw source=%s path=%s", source, path)
        return str(path.relative_to(self.data_root))

    def write_index(self, name: str, payload: object) -> None:
        self._write_json(self.indexes_root / name, payload)

    def _append_unique_models(self, path: Path, models: list[BaseModel]) -> None:
        with self._lock:
            existing = self._read_json(path, [])
            seen = {json.dumps(item, sort_keys=True) for item in existing}
            for model in models:
                payload = model.model_dump(mode="json")
                key = json.dumps(payload, sort_keys=True)
                if key not in seen:
                    existing.append(payload)
                    seen.add(key)
            self._write_json(path, existing)

    def _append_unique_dicts(self, path: Path, items: list[dict[str, object]]) -> None:
        with self._lock:
            existing = self._read_json(path, [])
            seen = {
                json.dumps(item, sort_keys=True)
                for item in existing
                if isinstance(item, dict)
            }
            for item in items:
                key = json.dumps(item, sort_keys=True)
                if key not in seen:
                    existing.append(item)
                    seen.add(key)
            self._write_json(path, existing)

    def _write_model(self, path: Path, model: BaseModel) -> None:
        self._write_json(path, model.model_dump(mode="json"))

    def _write_json(self, path: Path, payload: object) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = path.with_suffix(path.suffix + ".tmp")
            temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
            for attempt in range(5):
                try:
                    temp_path.replace(path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.2 * (attempt + 1))

    def _read_json(self, path: Path, default: object) -> object:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            logger.warning("invalid json path=%s error=%s", path, exc)
            return default

    def _safe_key(self, key: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key).strip("_")
        digest = hashlib.sha256(key.encode("utf-8", errors="ignore")).hexdigest()[:12]
        return f"{safe[:100] or 'item'}-{digest}"
