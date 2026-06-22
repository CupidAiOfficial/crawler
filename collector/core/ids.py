from __future__ import annotations

import hashlib
import re
import unicodedata


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).lower().strip()
    return re.sub(r"\s+", " ", text)


def entity_id(name: str, locality: str | None = None, lat: float | None = None, lon: float | None = None) -> str:
    geo = ""
    if lat is not None and lon is not None:
        geo = f"{round(lat, 4)}:{round(lon, 4)}"
    seed = "|".join(part for part in [normalize_text(name), normalize_text(locality), geo] if part)
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    slug = re.sub(r"[^a-z0-9]+", "-", normalize_text(name).replace(" ", "-")).strip("-")
    return f"{slug[:48] or 'entity'}-{digest}"


def signal_id(source: str, text: str, url: str | None = None) -> str:
    seed = f"{source}|{url or ''}|{normalize_text(text)[:400]}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]
