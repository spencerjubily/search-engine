from functools import lru_cache

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    searxng_url: HttpUrl = "http://localhost:8080"
    ollama_url: HttpUrl = "http://localhost:11434"
    ollama_model: str = "qwen2.5:14b"
    tavily_api_key: SecretStr | None = None
    agent_api_key: SecretStr | None = None
    paper_mcp_enabled: bool = True
    request_timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_search_results: int = Field(default=12, ge=1, le=30)
    max_concurrent_requests: int = Field(default=8, ge=1, le=64)
    allowed_origins: str = "http://localhost:3000,http://localhost:8000"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
