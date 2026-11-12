"""All runtime settings in one place.

Each field can be overridden by an environment variable of the same name
(case-insensitive), e.g. DATABASE_URL or MAX_ATTEMPTS. Values are read in
this order, highest priority first:
    1. real environment variables
    2. a local .env file (never committed)
    3. the defaults below
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Storage
    database_url: str = "sqlite:///./relay.db"
    redis_url: str = "redis://localhost:6379/0"

    # Stripe. Empty defaults on purpose: secrets must come from the environment.
    stripe_webhook_secret: str = ""
    stripe_api_key: str = ""
    signature_tolerance_seconds: int = 300

    # Delivery to the merchant's app
    downstream_url: str = "http://localhost:9000/events"
    delivery_timeout_seconds: float = 5.0

    # Retries. 12 attempts with 2s..2048s waits keep retrying for ~68 minutes,
    # so a typical outage heals on its own; anything longer goes to the DLQ.
    max_attempts: int = 12
    backoff_base_seconds: float = 2.0
    backoff_cap_seconds: float = 3600.0

    # Out-of-order handling
    parent_wait_seconds: int = 600
    parent_recheck_seconds: int = 5

    # Worker
    worker_batch_size: int = 50
    worker_poll_seconds: float = 1.0

    # Reconciliation
    reconcile_interval_seconds: int = 3600
    reconcile_default_lookback_seconds: int = 3 * 24 * 3600
    reconcile_overlap_seconds: int = 300
    reconcile_lock_ttl_seconds: int = 600

    # Admin API. Empty = no auth (fine on localhost, set it anywhere else).
    admin_token: str = ""


settings = Settings()
