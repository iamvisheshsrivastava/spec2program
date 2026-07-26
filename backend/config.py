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
    # Chosen by measurement, not reputation - see the model bake-off in the
    # README. Against the two sample specs this was the fastest model that
    # produced a rule-valid program on every run, and it is also cheaper per
    # generation than the reasoning models tried, because it emits ~790
    # completion tokens instead of ~4400.
    llm_model: str = "meta-llama/llama-3.3-70b-instruct"
    llm_temperature: float = 0.1
    llm_timeout: int = 60

    # Optional cap on the model's reply length. Left unset by default, and
    # you should think hard before setting it: reasoning models (including
    # the default deepseek-v4-flash) bill their hidden reasoning tokens
    # against this same budget, so a cap that looks generous next to the
    # ~1.2k tokens of actual JSON can still truncate mid-document. A single
    # observed run needed 8751 completion tokens to emit a 4.6 kB program.
    # Truncated JSON fails to parse and silently costs a whole LLM call, so
    # the default is "no cap".
    llm_max_tokens: int | None = None

    # How many vehicle specs a batch request may process concurrently. Each
    # one is a blocking LLM call, so this is I/O fan-out, not CPU
    # parallelism - raise it for faster fleets, lower it if the provider
    # starts rate-limiting.
    batch_max_workers: int = 8

    # Connection-pool sizing for the shared HTTP client used to call the LLM.
    llm_max_connections: int = 20

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
