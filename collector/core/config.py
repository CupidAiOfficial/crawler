from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    city_name: str = "Hyderabad"
    data_root: Path = Field(default=Path("data"))
    user_agent: str = "HyderabadCityKnowledgeCollector/0.1 contact=research-local"
    max_depth: int = 3
    max_candidates_per_run: int = 50
    request_delay_seconds: float = 1.2
    respect_robots: bool = True
    google_custom_search_api_key: str | None = None
    google_custom_search_engine_id: str | None = None
    web_search_results_per_query: int = 10
    web_page_max_links: int = 20
    web_page_max_chars: int = 12000


settings = CollectorSettings()
