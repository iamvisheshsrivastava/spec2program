"""Application configuration.

All settings are read from environment variables (or a local ``.env`` file).
See ``.env.example`` for the full list of options. Using pydantic-settings
keeps configuration type-safe and centralised, which makes the app easy to
deploy on any platform that injects environment variables.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application settings, populated from the environment."""

    # Which LLM backend to use:
    #   "mock"       -> offline, deterministic rule-based planner
    #   "openai"     -> any OpenAI-compatible Chat Completions endpoint
    #   "openrouter" -> OpenRouter.ai (a router in front of many model providers)
    llm_provider: str = "mock"

    # Credentials / endpoint for the OpenAI-compatible provider.
    # These are only required when llm_provider is "openai" or "openrouter".
    llm_api_key: str = ""
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_temperature: float = 0.1
    llm_timeout: int = 60

    # OpenRouter asks callers to identify their app via these optional
    # headers (used for their public leaderboard / rate-limit attribution).
    # Not secret, safe to leave at defaults.
    openrouter_site_url: str = "https://github.com/iamvisheshsrivastava/spec2program"
    openrouter_site_name: str = "spec2program"

    # Comma-separated list of allowed CORS origins ("*" allows all).
    cors_origins: str = "*"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @property
    def cors_origin_list(self) -> list[str]:
        """Parse the comma-separated CORS origins into a list."""
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]


# A single, importable settings instance used across the app.
settings = Settings()
