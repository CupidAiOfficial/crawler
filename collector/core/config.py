from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CollectorSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    city_name: str = "Hyderabad"
    data_root: Path = Field(default=Path("data"))
    user_agent: str = "CupidAIHyderabadCityCollector/0.1 (https://github.com/CupidAiOfficial/crawler; contact: CupidAiOfficial)"
    max_depth: int = 3
    max_candidates_per_run: int = 50
    crawler_workers: int = 4
    request_delay_seconds: float = 1.2
    max_retry_after_seconds: int = 120
    respect_robots: bool = True
    google_custom_search_api_key: str | None = None
    google_custom_search_engine_id: str | None = None
    web_search_results_per_query: int = 10
    web_page_max_links: int = 20
    web_page_max_chars: int = 12000
    web_page_min_text_chars: int = 500
    firecrawl_base_url: str = "http://localhost:3002"
    firecrawl_api_key: str | None = None
    firecrawl_search_limit: int = 10
    firecrawl_scrape_formats: list[str] = Field(default_factory=lambda: ["markdown", "html"])
    database_url: str | None = None
    log_level: str = "INFO"
    image_download_timeout_seconds: float = 20.0
    image_download_max_bytes: int = 5_000_000


settings = CollectorSettings()
