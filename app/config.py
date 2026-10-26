from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="WM_", extra="ignore", env_ignore_empty=True)

    secret_key: str = "dev-secret-change-me"
    database_url: str = "sqlite:///./wallet_monitor.db"
    subtensor_url: str = "wss://entrypoint-finney.opentensor.ai:443"
    # Archive node (keeps historical state) used to backfill past blocks.
    archive_url: str = "wss://archive.chain.opentensor.ai:443"

    # Transfers worth less than this (TAO, or TAO value for stake transfers) are ignored.
    min_transfer_tao: float = 0.2

    taostats_api_key: str = ""
    taostats_base_url: str = "https://api.taostats.io"
    taostats_cache_seconds: int = 300

    watcher_enabled: bool = True
    watcher_start_block: int | None = None
    watcher_poll_seconds: int = 6
    watcher_batch_size: int = 20
    watcher_concurrency: int = 5  # blocks fetched at once while catching up

    # How far back the one-off history backfill scans (7200 blocks ≈ 1 day).
    backfill_days: int = 7
    backfill_batch_size: int = 100  # blocks per progress/commit step
    backfill_concurrency: int = 10  # blocks fetched at once (halved automatically when rate-limited)

    balance_refresh_seconds: int = 300

    @property
    def min_transfer_rao(self) -> int:
        return int(self.min_transfer_tao * 10**9)


@lru_cache
def get_settings() -> Settings:
    return Settings()
