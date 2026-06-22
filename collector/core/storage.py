from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from pydantic import BaseModel

from collector.core.models import CityEntity, CrawlCandidate, EntityMetadata, Relationship, TextSignal


class JsonStore:
    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root
        self.city_root = data_root / "city"
        self.entities_root = self.city_root / "entities"
        self.indexes_root = self.city_root / "indexes"
        self.raw_root = data_root / "raw"
        self.checkpoint_root = data_root / "checkpoints"
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
        self._write_model(folder / "entity.json", entity)
        if not (folder / "reviews.json").exists():
            self._write_json(folder / "reviews.json", [])
        if not (folder / "comments.json").exists():
            self._write_json(folder / "comments.json", [])
        if not (folder / "intents.json").exists():
            self._write_json(folder / "intents.json", entity.metadata.suitability_scores)
        if not (folder / "relationships.json").exists():
            self._write_json(folder / "relationships.json", [])
        self._write_model(folder / "metadata.json", entity.metadata)

    def load_entity(self, entity_id: str) -> CityEntity | None:
        path = self.entities_root / entity_id / "entity.json"
        if not path.exists():
            return None
        return CityEntity.model_validate_json(path.read_text(encoding="utf-8"))

    def iter_entities(self) -> Iterable[CityEntity]:
        for path in sorted(self.entities_root.glob("*/entity.json")):
            yield CityEntity.model_validate_json(path.read_text(encoding="utf-8"))

    def append_reviews(self, entity_id: str, reviews: list[TextSignal]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "reviews.json", reviews)

    def append_comments(self, entity_id: str, comments: list[TextSignal]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "comments.json", comments)

    def append_relationships(self, entity_id: str, relationships: list[Relationship]) -> None:
        self._append_unique_models(self.entity_dir(entity_id) / "relationships.json", relationships)

    def save_metadata(self, entity_id: str, metadata: EntityMetadata) -> None:
        self._write_model(self.entity_dir(entity_id) / "metadata.json", metadata)

    def save_frontier(self, candidates: list[CrawlCandidate]) -> None:
        self._write_json(
            self.checkpoint_root / "frontier.json",
            [candidate.model_dump(mode="json") for candidate in candidates],
        )

    def load_frontier(self) -> list[CrawlCandidate]:
        path = self.checkpoint_root / "frontier.json"
        if not path.exists():
            return []
        return [CrawlCandidate.model_validate(item) for item in self._read_json(path, [])]

    def save_raw(self, source: str, key: str, payload: object) -> str:
        folder = self.raw_root / source
        folder.mkdir(parents=True, exist_ok=True)
        safe_key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)[:120]
        path = folder / f"{safe_key}.json"
        self._write_json(path, payload)
        return str(path.relative_to(self.data_root))

    def write_index(self, name: str, payload: object) -> None:
        self._write_json(self.indexes_root / name, payload)

    def _append_unique_models(self, path: Path, models: list[BaseModel]) -> None:
        existing = self._read_json(path, [])
        seen = {json.dumps(item, sort_keys=True) for item in existing}
        for model in models:
            payload = model.model_dump(mode="json")
            key = json.dumps(payload, sort_keys=True)
            if key not in seen:
                existing.append(payload)
                seen.add(key)
        self._write_json(path, existing)

    def _write_model(self, path: Path, model: BaseModel) -> None:
        self._write_json(path, model.model_dump(mode="json"))

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def _read_json(self, path: Path, default: object) -> object:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
