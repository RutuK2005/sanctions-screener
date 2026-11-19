"""Runtime settings, read from environment variables (or a local .env file)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres@127.0.0.1:54329/screener"
    api_key: str = "dev-key"
    # Score at/above which a hit is reported and a case is opened for review.
    # 0.88 = best F1 in scripts/eval.py on the full OFAC SDN list (see README).
    match_threshold: float = 0.88
    # Candidates pulled from Postgres before re-scoring in Python.
    candidate_limit: int = 150
    # pg_trgm similarity floor for the GIN-indexed `%` operator.
    trgm_threshold: float = 0.25
    anthropic_api_key: str | None = None
    summary_model: str = "claude-sonnet-5-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
