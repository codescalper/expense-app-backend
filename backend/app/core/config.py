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
# Vercel Functions reject request bodies above 4.5 MB (FUNCTION_PAYLOAD_TOO_LARGE).
VERCEL_MAX_BODY_BYTES = 4_500_000


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

    # --- Serverless / Vercel (all opt-in; docker-compose/self-host leaves them at defaults) ---
    # Short-lived instances: no SQLAlchemy connection pool (NullPool), nothing kept between
    # invocations. Use with Neon's pooled (-pooler) URL and DB_PGBOUNCER=true.
    serverless: bool = False
    # queue: requests only enqueue send_email jobs; a worker / the cron tick sends them.
    # inline: the same jobs are also delivered right after the response (FastAPI background
    # task) by the instance that handled the request; failures stay queued for the cron tick.
    email_delivery: Literal["queue", "inline"] = "queue"
    # Enables GET/POST /internal/cron/tick (Authorization: Bearer <CRON_SECRET>); unset = 404.
    cron_secret: SecretStr | None = None
    cron_time_budget_seconds: float = Field(default=50.0, ge=5, le=800)
    # local: files under UPLOAD_DIR. s3: a private bucket on an S3-compatible service (e.g.
    # Neon object storage; path-style, SigV4). vercel_blob: a PRIVATE Vercel Blob store.
    storage_backend: Literal["local", "s3", "vercel_blob"] = "local"
    s3_endpoint_url: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: SecretStr | None = None
    s3_region: str = "us-east-1"
    s3_bucket: str | None = None
    blob_read_write_token: SecretStr | None = None
    blob_api_url: str = "https://vercel.com/api/blob"
    # Take the client IP from Vercel's proxy headers (x-vercel-forwarded-for / x-real-ip /
    # x-forwarded-for). Only safe when every request reaches the app through Vercel.
    trust_vercel_proxy: bool = False
    # Set by the Vercel runtime itself (VERCEL=1); never set it by hand.
    vercel: bool = False

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
        self._check_serverless()
        return self

    def _check_serverless(self) -> None:
        if self.storage_backend == "vercel_blob" and not self.blob_read_write_token:
            raise ValueError("STORAGE_BACKEND=vercel_blob requires BLOB_READ_WRITE_TOKEN")
        if self.storage_backend == "s3":
            missing_s3 = [
                name
                for name, value in (
                    ("S3_ENDPOINT_URL", self.s3_endpoint_url),
                    ("S3_ACCESS_KEY_ID", self.s3_access_key_id),
                    ("S3_SECRET_ACCESS_KEY", self.s3_secret_access_key),
                    ("S3_BUCKET", self.s3_bucket),
                    ("S3_REGION", self.s3_region),
                )
                if not value
            ]
            if missing_s3:
                raise ValueError("STORAGE_BACKEND=s3 requires " + ", ".join(missing_s3))
            endpoint = self.s3_endpoint_url or ""
            if self.is_production_like and not endpoint.startswith("https://"):
                raise ValueError("S3_ENDPOINT_URL must use https:// outside development")
        if self.cron_secret is not None:
            secret = self.cron_secret.get_secret_value()
            if len(secret) < 16 or not secret.isascii() or not secret.isalnum():
                raise ValueError(
                    "CRON_SECRET must be at least 16 letters/digits (it is sent in an HTTP header)"
                )
        if self.vercel and self.is_production_like:
            # Production on Vercel: these are not optional. Local disk is wiped between
            # invocations, there is no long-running worker (queued emails would wait for the
            # next cron tick, past their codes' expiry) and no stable connection-pool lifetime.
            missing = [
                name
                for name, ok in (
                    ("SERVERLESS=true", self.serverless),
                    ("EMAIL_DELIVERY=inline", self.email_delivery == "inline"),
                    (
                        "STORAGE_BACKEND=s3 or vercel_blob",
                        self.storage_backend in ("s3", "vercel_blob"),
                    ),
                    ("CRON_SECRET", self.cron_secret is not None),
                )
                if not ok
            ]
            if missing:
                raise ValueError(
                    "Running on Vercel in "
                    + self.environment
                    + " requires "
                    + ", ".join(missing)
                    + " (see docs/VERCEL.md)"
                )

    def deployment_warnings(self) -> list[str]:
        """Non-fatal configuration problems, logged once at start-up."""
        warnings: list[str] = []
        if self.vercel:
            if self.email_delivery != "inline":
                warnings.append(
                    "EMAIL_DELIVERY=queue on Vercel: emails are only sent by the cron tick "
                    "(daily on Hobby). Set EMAIL_DELIVERY=inline"
                )
            if self.cron_secret is None:
                warnings.append(
                    "CRON_SECRET is unset on Vercel: /internal/cron/tick is disabled, so "
                    "housekeeping, push notifications and reminders never run"
                )
            if not self.trust_vercel_proxy:
                warnings.append(
                    "TRUST_VERCEL_PROXY is off on Vercel: per-IP rate limits and audit logs may "
                    "not see the real client address"
                )
            if self.max_receipt_bytes > VERCEL_MAX_BODY_BYTES - 64 * 1024:
                warnings.append(
                    "MAX_RECEIPT_BYTES is above what Vercel accepts in one request (4.5 MB); "
                    "larger uploads fail with 413 before reaching the app. Use 4194304"
                )
            if not self.serverless:
                warnings.append("SERVERLESS is off on Vercel: set SERVERLESS=true")
            if self.storage_backend == "local":
                warnings.append(
                    "STORAGE_BACKEND=local on Vercel: uploaded files are lost between invocations"
                )
        elif self.trust_vercel_proxy:
            warnings.append(
                "TRUST_VERCEL_PROXY=true outside Vercel: clients can spoof their IP with "
                "X-Forwarded-For unless a proxy you control overwrites it"
            )
        if self.serverless and not self.db_pgbouncer:
            warnings.append(
                "SERVERLESS=true without DB_PGBOUNCER=true: every request opens a direct "
                "connection; use Neon's pooled (-pooler) URL with DB_PGBOUNCER=true"
            )
        return warnings


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
