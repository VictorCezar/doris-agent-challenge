"""Configuração via variáveis de ambiente (ADR-002/ADR-004)."""
from __future__ import annotations

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    PROXY_URL: str = "http://proxy:3000"
    # SecretStr: nunca aparece em repr/logs.
    OPENROUTER_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api/v1"
    DATA_DIR: str = "/data"
    TURN_DEADLINE_S: float = 80.0
    ROUTER_MODEL: str = "openai/gpt-6-luna"
    MAIN_MODEL: str = "google/gemini-3.8-flash"
    FALLBACK_MODEL: str = "openai/gpt-6-luna"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
