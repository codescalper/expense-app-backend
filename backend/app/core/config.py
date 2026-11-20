"""Application settings, loaded from environment variables (and an optional .env file).

Nothing secret has a usable default: DATABASE_URL and JWT_SECRET_KEY must be provided.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "staging", "production"]

_INSECURE_SECRET_MARKERS = ("change-me", "change_me", "replace_me", "replace-me", "dev-only")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- App ---------------------------------------------------------------
    environment: Environment = "development"
    app_name: str = "Family Budget API"
    api_v1_prefix: str = "/api/v1"
    enable_docs: bool = True
    cors_origins: str = ""  # comma-separated allowlist
    # Proxies whose X-Forwarded-For/Proto uvicorn trusts. uvicorn reads the same
    # FORWARDED_ALLOW_IPS environment variable itself; it is declared here so a dangerous
    # value stops the app from starting.
    forwarded_allow_ips: str = "127.0.0.1"

    # --- Database ----------------------------------------------------------
    database_url: str
    database_migration_url: str | None = None  # direct (non-pooled) URL for Alembic
    db_pgbouncer: bool = False  # Neon "-pooler" / PgBouncer transaction pooling
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # --- Background worker (PostgreSQL-backed job queue) --------------------
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 10
    job_max_attempts: int = 5

    # --- Auth --------------------------------------------------------------
    jwt_secret_key: SecretStr
    jwt_issuer: str = "family-budget"
    access_token_expire_minutes: int = Field(default=15, ge=1, le=60)
    refresh_token_expire_days: int = Field(default=7, ge=1, le=90)
    # Hard cap on a session's age (from sign-in), however often it is refreshed.
    session_absolute_lifetime_days: int = Field(default=60, ge=1, le=365)
    email_code_expire_minutes: int = Field(default=30, ge=5, le=24 * 60)
    email_code_max_attempts: int = 5
    # Argon2id cost (RFC 9106 "second recommended" profile by default). Tests lower these.
    argon2_time_cost: int = 3
    argon2_memory_kib: int = 64 * 1024
    argon2_parallelism: int = 4
    # Max concurrent Argon2 hash/verify operations per process (each uses argon2_memory_kib).
    password_hash_concurrency: int = Field(default=2, ge=1, le=32)

    # --- Abuse protection --------------------------------------------------
    rate_limit_enabled: bool = True
    auth_ip_rate_limit: int = 30  # requests per window per IP, per auth endpoint
    auth_ip_rate_window_seconds: int = 300
    login_max_failures: int = 5  # per email (hash), decaying over the window below
    login_failure_window_seconds: int = 900
    login_lockout_seconds: int = 900  # how long an email stays locked once the max is hit
    email_send_limit: int = 3  # verification/reset emails per address per window
    email_send_window_seconds: int = 900
    # Wrong one-time codes per account and purpose over 24 h, across re-issued codes.
    email_code_daily_max_failures: int = 15
    # Re-authentication (change password / delete account): requests and wrong passwords
    # per user inside the window.
    reauth_rate_limit: int = 10
    reauth_max_failures: int = 5
    reauth_window_seconds: int = 900
    avatar_upload_limit: int = 10  # uploads per user per window
    avatar_upload_window_seconds: int = 3600
    receipt_upload_limit: int = 30  # receipt uploads per user per window
    receipt_upload_window_seconds: int = 3600
    # UPI: new payments (initiations; idempotent replays don't count) and QR parses per user.
    upi_initiate_rate_limit: int = 30
    upi_initiate_rate_window_seconds: int = 600
    upi_parse_rate_limit: int = 120
    upi_parse_rate_window_seconds: int = 600
    # Per-user limits on expensive reads: CSV exports (transactions + reports) and the
    # analytics endpoints (/reports/*, /dashboard); push-token registrations.
    export_rate_limit: int = 20
    export_rate_window_seconds: int = 600
    analytics_rate_limit: int = 300
    analytics_rate_window_seconds: int = 300
    device_register_rate_limit: int = 20
    device_register_rate_window_seconds: int = 3600

    # --- Email -------------------------------------------------------------
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = False
    smtp_use_tls: bool = False
    email_from: str = "Family Budget <no-reply@familybudget.local>"

    # --- Files -------------------------------------------------------------
    upload_dir: str = "./data/uploads"
    max_avatar_bytes: int = 2 * 1024 * 1024
    max_receipt_bytes: int = Field(default=5 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)

    # --- Push notifications (Expo) -------------------------------------------
    # false = pending pushes are marked "skipped" (in-app notifications still work).
    push_enabled: bool = True
    expo_push_url: str = "https://exp.host/--/api/v2/push/send"
    expo_access_token: SecretStr | None = None  # only if "enhanced push security" is on
    expo_push_timeout_seconds: float = Field(default=15.0, gt=0, le=60)

    @property
    def is_production_like(self) -> bool:
        return self.environment in ("staging", "production")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def forwarded_allow_ips_list(self) -> list[str]:
        return [o.strip() for o in self.forwarded_allow_ips.split(",") if o.strip()]

    @model_validator(mode="after")
    def _check_secrets(self) -> Settings:
        secret = self.jwt_secret_key.get_secret_value()
        if len(secret) < 32:
            raise ValueError("JWT_SECRET_KEY must be at least 32 characters")
        if self.is_production_like:
            if any(m in secret.lower() for m in _INSECURE_SECRET_MARKERS):
                raise ValueError("JWT_SECRET_KEY looks like a placeholder; set a real secret")
            if "*" in self.cors_origin_list:
                raise ValueError("Wildcard CORS origin is not allowed outside development")
            if "*" in self.forwarded_allow_ips_list:
                raise ValueError(
                    "FORWARDED_ALLOW_IPS='*' trusts X-Forwarded-For from any client (IP "
                    "spoofing defeats per-IP limits); list your proxy addresses instead"
                )
        return self


def normalize_async_db_url(raw_url: str) -> tuple[str, dict[str, Any]]:
    """Turn a libpq-style URL (as Neon hands out) into an asyncpg SQLAlchemy URL + connect args.

    asyncpg does not understand ``sslmode``/``channel_binding`` query params, so they are
    stripped and ``sslmode`` is mapped onto asyncpg's ``ssl`` argument.
    """
    parts = urlsplit(raw_url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql", "postgresql+psycopg", "postgresql+psycopg2"):
        scheme = "postgresql+asyncpg"
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    connect_args: dict[str, Any] = {}
    sslmode = query.pop("sslmode", None) or query.pop("ssl", None)
    query.pop("channel_binding", None)
    if sslmode and sslmode != "disable":
        connect_args["ssl"] = sslmode  # asyncpg accepts libpq-style mode strings
    url = urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return url, connect_args


@lru_cache
def get_settings() -> Settings:
    return Settings()  # values come from the environment
