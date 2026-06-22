from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from collector.core.config import settings
from collector.core.http import PoliteHttpClient
from collector.core.ids import entity_id
from collector.core.models import CandidateKind, CityEntity, CrawlCandidate, SourceRecord
from collector.core.orchestrator import SourceAdapter
from collector.core.storage import JsonStore


HYDERABAD_TERMS = {
    "hyderabad",
    "secunderabad",
    "telangana",
    "hitec city",
    "gachibowli",
    "madhapur",
    "jubilee hills",
    "banjara hills",
    "begumpet",
    "kondapur",
    "ameerpet",
    "charminar",
}

CATEGORY_HINTS = {
    "restaurant": ["restaurant", "food", "cafe", "biryani", "bar", "pub"],
    "event": ["event", "festival", "concert", "workshop", "meetup", "screening"],
    "community": ["community", "club", "group", "volunteer", "ngo", "foundation"],
    "attraction": ["landmark", "tourism", "museum", "park", "lake", "temple", "fort"],
    "business": ["coworking", "startup", "company", "business", "store", "shop"],
    "activity": ["badminton", "football", "gaming", "fitness", "yoga", "cricket"],
}


class WebPageAdapter(SourceAdapter):
    name = "web_page"

    def __init__(self, http: PoliteHttpClient, store: JsonStore) -> None:
        self.http = http
        self.store = store

    def can_handle(self, candidate: CrawlCandidate) -> bool:
        return candidate.source == self.name and candidate.kind == CandidateKind.SOURCE_URL

    def crawl(self, candidate: CrawlCandidate) -> tuple[list[CityEntity], list[CrawlCandidate]]:
        result = self.http.get_text(candidate.value)
        content_type = result.content_type or ""
        if "html" not in content_type and "<html" not in result.text[:500].lower():
            return [], []
        soup = BeautifulSoup(result.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = self._clean_text(soup.get_text(" "))
        if not self._is_hyderabad_relevant(text, candidate.value):
            return [], []
        raw_path = self.store.save_raw(
            self.name,
            self._raw_key(candidate.value),
            {
                "url": result.url,
                "status_code": result.status_code,
                "content_type": content_type,
                "title": self._title(soup),
                "text": text[: settings.web_page_max_chars],
                "metadata": candidate.metadata,
            },
        )
        entities = self._entities_from_page(soup, text, result.url, raw_path)
        new_candidates = self._link_candidates(soup, result.url, candidate, text)
        return entities, new_candidates

    def _entities_from_page(
        self, soup: BeautifulSoup, text: str, url: str, raw_path: str
    ) -> list[CityEntity]:
        structured = self._json_ld_entities(soup, url, raw_path)
        if structured:
            return structured
        title = self._title(soup)
        if not title:
            return []
        description = self._meta(soup, "description") or text[:500]
        category = self._category(title, description, text)
        locality = self._locality(text)
        return [
            CityEntity(
                id=entity_id(title, locality),
                name=title,
                category=category,
                description=description,
                locality=locality,
                website=url,
                social_links=self._social_links(soup, url),
                sources=[
                    SourceRecord(
                        source=self.name,
                        url=url,
                        raw_path=raw_path,
                        metadata={
                            "title": title,
                            "description": description,
                            "image_urls": self._image_urls(soup, url),
                        },
                    )
                ],
            )
        ]

    def _json_ld_entities(self, soup: BeautifulSoup, url: str, raw_path: str) -> list[CityEntity]:
        entities: list[CityEntity] = []
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string or script.get_text()
            if not raw.strip():
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            nodes = payload if isinstance(payload, list) else [payload]
            for node in nodes:
                for item in self._flatten_json_ld(node):
                    entity = self._entity_from_json_ld(item, url, raw_path)
                    if entity:
                        entities.append(entity)
        return entities

    def _flatten_json_ld(self, node: object) -> list[dict[str, object]]:
        if not isinstance(node, dict):
            return []
        out = [node]
        graph = node.get("@graph")
        if isinstance(graph, list):
            out.extend(item for item in graph if isinstance(item, dict))
        return out

    def _entity_from_json_ld(
        self, item: dict[str, object], url: str, raw_path: str
    ) -> CityEntity | None:
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        item_type = item.get("@type")
        if isinstance(item_type, list):
            item_type = item_type[0] if item_type else None
        category = str(item_type or "web_entity")
        address = item.get("address")
        address_text = self._address_text(address)
        geo = item.get("geo") if isinstance(item.get("geo"), dict) else {}
        lat = self._float_or_none(geo.get("latitude")) if isinstance(geo, dict) else None
        lon = self._float_or_none(geo.get("longitude")) if isinstance(geo, dict) else None
        description = item.get("description") if isinstance(item.get("description"), str) else None
        same_as = item.get("sameAs")
        social_links = self._same_as_links(same_as)
        locality = self._locality(" ".join(part for part in [address_text, description or ""] if part))
        return CityEntity(
            id=entity_id(name, locality, lat, lon),
            name=name,
            category=category,
            description=description,
            locality=locality,
            address=address_text,
            latitude=lat,
            longitude=lon,
            website=str(item.get("url") or url),
            social_links=social_links,
            sources=[
                SourceRecord(
                    source=self.name,
                    url=url,
                    raw_path=raw_path,
                    metadata={"json_ld_type": item_type, "json_ld": item},
                )
            ],
        )

    def _link_candidates(
        self, soup: BeautifulSoup, base_url: str, candidate: CrawlCandidate, text: str
    ) -> list[CrawlCandidate]:
        candidates: list[CrawlCandidate] = []
        base_domain = urlparse(base_url).netloc.lower()
        for anchor in soup.find_all("a", href=True):
            if len(candidates) >= settings.web_page_max_links:
                break
            label = self._clean_text(anchor.get_text(" "))
            href = urljoin(base_url, anchor["href"])
            parsed = urlparse(href)
            if parsed.scheme not in {"http", "https"}:
                continue
            if parsed.netloc.lower() != base_domain and not self._is_hyderabad_relevant(label, href):
                continue
            combined = f"{label} {href}"
            if not self._looks_useful_link(combined):
                continue
            candidates.append(
                CrawlCandidate(
                    kind=CandidateKind.SOURCE_URL,
                    source=self.name,
                    value=href,
                    priority=max(0.05, candidate.priority - 0.15),
                    depth=candidate.depth + 1,
                    metadata={"parent_url": base_url, "anchor": label[:160]},
                )
            )
        mentioned_queries = self._mentioned_queries(text)
        for query in mentioned_queries:
            candidates.append(
                CrawlCandidate(
                    kind=CandidateKind.QUERY,
                    source="google_search",
                    value=query,
                    priority=0.25,
                    depth=candidate.depth + 1,
                    metadata={"parent_url": base_url, "reason": "extracted_mention"},
                )
            )
        return candidates

    def _mentioned_queries(self, text: str) -> list[str]:
        matches = re.findall(
            r"\b([A-Z][A-Za-z0-9&'. -]{2,70}\s(?:Hyderabad|Secunderabad|Telangana))\b",
            text[: settings.web_page_max_chars],
        )
        out: list[str] = []
        seen: set[str] = set()
        for match in matches:
            value = self._clean_text(match)
            key = value.lower()
            if key not in seen:
                out.append(value)
                seen.add(key)
            if len(out) >= 8:
                break
        return out

    def _looks_useful_link(self, text: str) -> bool:
        lower = text.lower()
        useful = [
            "hyderabad",
            "event",
            "venue",
            "places",
            "things-to-do",
            "restaurant",
            "cafe",
            "community",
            "festival",
            "tourism",
            "calendar",
        ]
        blocked = ["login", "signup", "privacy", "terms", "javascript:", "mailto:", "tel:"]
        return any(item in lower for item in useful) and not any(item in lower for item in blocked)

    def _title(self, soup: BeautifulSoup) -> str | None:
        og_title = self._meta(soup, "og:title")
        if og_title:
            return og_title
        if soup.title and soup.title.string:
            return self._clean_text(soup.title.string)
        h1 = soup.find("h1")
        return self._clean_text(h1.get_text(" ")) if h1 else None

    def _meta(self, soup: BeautifulSoup, name: str) -> str | None:
        tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        content = tag.get("content") if tag else None
        return self._clean_text(content) if isinstance(content, str) else None

    def _category(self, title: str, description: str, text: str) -> str:
        haystack = f"{title} {description} {text[:2000]}".lower()
        for category, hints in CATEGORY_HINTS.items():
            if any(re.search(rf"(?<![a-z0-9]){re.escape(hint)}(?![a-z0-9])", haystack) for hint in hints):
                return category
        return "web_entity"

    def _locality(self, text: str) -> str | None:
        lower = text.lower()
        for term in HYDERABAD_TERMS:
            if term in lower and term != "hyderabad":
                return term.title()
        if "hyderabad" in lower:
            return "Hyderabad"
        return None

    def _is_hyderabad_relevant(self, text: str, url: str) -> bool:
        lower = f"{text} {url}".lower()
        return any(term in lower for term in HYDERABAD_TERMS)

    def _clean_text(self, value: str | None) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    def _raw_key(self, url: str) -> str:
        parsed = urlparse(url)
        return f"{parsed.netloc}-{parsed.path}".strip("-/") or parsed.netloc

    def _social_links(self, soup: BeautifulSoup, base_url: str) -> dict[str, str]:
        links: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = urljoin(base_url, anchor["href"])
            domain = urlparse(href).netloc.lower()
            for key in ["instagram", "facebook", "youtube", "twitter", "x.com", "linkedin"]:
                if key in domain:
                    links[key.replace(".com", "")] = href
        return links

    def _same_as_links(self, same_as: object) -> dict[str, str]:
        values: list[str] = []
        if isinstance(same_as, str):
            values = [same_as]
        elif isinstance(same_as, list):
            values = [str(item) for item in same_as]
        out: dict[str, str] = {}
        for value in values:
            domain = urlparse(value).netloc.lower()
            key = domain.replace("www.", "").split(".")[0]
            if key:
                out[key] = value
        return out

    def _image_urls(self, soup: BeautifulSoup, base_url: str) -> list[str]:
        urls: list[str] = []
        for image in soup.find_all("img", src=True):
            urls.append(urljoin(base_url, image["src"]))
            if len(urls) >= 20:
                break
        og_image = self._meta(soup, "og:image")
        if og_image:
            urls.insert(0, urljoin(base_url, og_image))
        return list(dict.fromkeys(urls))

    def _address_text(self, address: object) -> str | None:
        if isinstance(address, str):
            return address
        if not isinstance(address, dict):
            return None
        parts = [
            address.get("streetAddress"),
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("postalCode"),
            address.get("addressCountry"),
        ]
        value = ", ".join(str(part) for part in parts if part)
        return value or None

    def _float_or_none(self, value: object) -> float | None:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
