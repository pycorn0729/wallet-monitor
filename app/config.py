from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WM_", extra="ignore", env_ignore_empty=True)

    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./wallet_monitor.db"
    subtensor_url: str = "wss://entrypoint-finney.opentensor.ai:443"

    taostats_api_key: str = ""
    taostats_base_url: str = "https://api.taostats.io"
    taostats_cache_seconds: int = 300

    watcher_enabled: bool = True
    watcher_start_block: int | None = None
    watcher_poll_seconds: int = 6
    watcher_batch_size: int = 20

    balance_refresh_seconds: int = 300


@lru_cache
def get_settings() -> Settings:
    return Settings()
