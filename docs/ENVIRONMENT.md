# Environment variables

The backend reads settings with pydantic-settings (`backend/app/core/config.py`). Rules:

- Names are case-insensitive. The env var is the field name in upper case
  (`jwt_secret_key` → `JWT_SECRET_KEY`).
- Values come from real environment variables first, then from `.env` or `../.env` relative to
  the **current working directory**. So running from `backend/` picks up the repo-root `.env`.
  Unknown variables are ignored.
- Booleans accept `true/false`, `1/0`, `yes/no`, `on/off`.
- Settings are validated at start-up. Invalid values stop the process with a clear error.

Template: [`/.env.example`](../.env.example). Never commit a real `.env`.

## Backend settings (`app/core/config.py`)

### App

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `ENVIRONMENT` | `development` | One of `development`, `test`, `staging`, `production`. `staging`/`production` ("production-like") turn on JSON logs, HSTS, the placeholder-secret check and the wildcard-CORS ban. `production` also force-disables the OpenAPI docs. `test` uses no connection pool (`NullPool`) | `production` (or `staging`) |
| `APP_NAME` | `Family Budget API` | OpenAPI title | Leave as is |
| `API_V1_PREFIX` | `/api/v1` | Mount point of the versioned API | Leave as is (the mobile app hard-codes `/api/v1`) |
| `ENABLE_DOCS` | `true` | Serve `/docs`, `/redoc` and `/openapi.json`. Always off when `ENVIRONMENT=production` | Set `false` on any internet-facing staging |
| `CORS_ORIGINS` | *(empty)* | Comma-separated origin allowlist. Empty means no CORS middleware at all. `*` is rejected in staging/production | Empty unless a web client needs it. Native apps don't use CORS |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Proxies whose `X-Forwarded-For`/`X-Forwarded-Proto` are trusted. **uvicorn reads the same variable itself** (with `--proxy-headers`); it is also declared in `Settings` so that `*` stops the app from starting in staging/production (it would let any client spoof its IP and dodge the per-IP limits) | Your reverse proxy's address(es) as seen from the API container, comma-separated. See [DEPLOYMENT.md](DEPLOYMENT.md#5-reverse-proxy-https) |

### Database

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `DATABASE_URL` | **required** | Runtime DB URL. libpq-style URLs (`postgres://`, `postgresql://`, `+psycopg`) are rewritten to `postgresql+asyncpg://`. `sslmode`/`ssl` and `channel_binding` query params are stripped and `sslmode` is passed to asyncpg as `ssl=<mode>` (`normalize_async_db_url`) | Neon **pooled** (`-pooler`) URL with `sslmode=require` (or `verify-full`). See [NEON.md](NEON.md) |
| `DATABASE_MIGRATION_URL` | *(unset)* | Used **only by Alembic**. Falls back to `DATABASE_URL` | Neon **direct** (non-pooler) URL, owner/migration role |
| `DB_PGBOUNCER` | `false` | Set when `DATABASE_URL` goes through a transaction-mode pooler (Neon `-pooler`, PgBouncer). Disables the asyncpg statement cache and uses unique prepared-statement names. Alembic always ignores this flag | `true` with a Neon pooled URL |
| `DB_POOL_SIZE` | `5` | SQLAlchemy pool size **per process** | Size so that `(pool + overflow) × uvicorn workers × replicas + worker` fits the DB/pooler limit |
| `DB_MAX_OVERFLOW` | `10` | Extra connections beyond the pool under load | As above |
| `DB_ECHO` | `false` | Log every SQL statement | Never `true` in production: logs statements and parameters, which bypass log redaction |

### Background worker

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `WORKER_POLL_INTERVAL_SECONDS` | `2.0` | Sleep between polls when no job was due. This sets email latency | Default is fine. On Neon, every poll keeps the compute awake (see [NEON.md](NEON.md#scale-to-zero)) |
| `WORKER_BATCH_SIZE` | `10` | Max jobs claimed per poll | Default |
| `JOB_MAX_ATTEMPTS` | `5` | Default retry budget for jobs enqueued without an explicit `max_attempts` (`JobRepository.enqueue`). Retries back off exponentially (15 s … 1 h) | Default |

### Auth

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `JWT_SECRET_KEY` | **required** | HS256 signing key for access tokens **and** the HMAC key for email codes. Must be at least 32 characters. In staging/production, values containing `change-me`, `change_me`, `replace_me`, `replace-me` or `dev-only` are rejected | Random, at least 48 bytes: `python -c "import secrets; print(secrets.token_urlsafe(48))"`. Different per environment. Keep it in a secret store. See [SECURITY.md](SECURITY.md#secrets) for rotation effects |
| `JWT_ISSUER` | `family-budget` | `iss` claim, checked on decode | Optional: set per environment (e.g. `family-budget-staging`) so tokens can't cross environments even with a shared key |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | Access JWT lifetime (1–60) | 15 |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | Session idle lifetime (1–90). Slides forward on every refresh, so a session ends after this many days **without use** | 7–30 depending on risk appetite |
| `SESSION_ABSOLUTE_LIFETIME_DAYS` | `60` | Hard cap on a session's age from sign-in (1–365), however often it is refreshed. Enforced on refresh, on every authenticated request and by the push sender (devices of over-age sessions get no pushes) | 30–90 |
| `EMAIL_CODE_EXPIRE_MINUTES` | `30` | Verification and reset code lifetime (5–1440) | 30 |
| `EMAIL_CODE_MAX_ATTEMPTS` | `5` | Wrong guesses before a code is burned | 5 |
| `ARGON2_TIME_COST` | `3` | Argon2id iterations | Default. Measure login latency on your hardware before raising it |
| `ARGON2_MEMORY_KIB` | `65536` | Argon2id memory per hash (64 MiB) | Default. Each concurrent login uses this much RAM |
| `ARGON2_PARALLELISM` | `4` | Argon2id lanes | Default |
| `PASSWORD_HASH_CONCURRENCY` | `2` | Max concurrent Argon2 hash/verify operations **per process** (1–32); extra logins queue. Bounds RAM to about this × `ARGON2_MEMORY_KIB` | Raise only with RAM to spare (each slot = 64 MiB by default) |

Changing the Argon2 parameters is safe. Existing hashes still verify and are re-hashed on the
user's next successful login.

### Abuse protection

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `RATE_LIMIT_ENABLED` | `true` | Master switch for every PostgreSQL-backed limit in this section (per-IP, per-email login failures and lockout, email quota, code failures, re-auth, uploads, UPI, exports/analytics, device registration) | Always `true` (the old per-account `users.locked_until` lockout is no longer used) |
| `AUTH_IP_RATE_LIMIT` | `30` | Requests per window per client IP, **per endpoint** (register, verify-email, resend-verification, login, refresh, forgot-password, reset-password) | Requires a correct client IP behind the proxy (`FORWARDED_ALLOW_IPS`). Households behind one NAT share it |
| `AUTH_IP_RATE_WINDOW_SECONDS` | `300` | Window for the above | Default |
| `LOGIN_MAX_FAILURES` | `5` | Failed logins per **email (keyed HMAC)** within the decaying window that lock sign-in for that email | Default |
| `LOGIN_FAILURE_WINDOW_SECONDS` | `900` | Decaying (sliding) failure window: failures fade out over two windows without new ones | Default |
| `LOGIN_LOCKOUT_SECONDS` | `900` | How long an email stays locked once `LOGIN_MAX_FAILURES` is reached (429 with Retry-After). Works for unknown emails too, so it reveals nothing | Default. Accepted trade-off: anyone can lock sign-in for a known email this long ([SECURITY.md](SECURITY.md)) |
| `EMAIL_SEND_LIMIT` | `3` | Verification/reset/"account exists" emails per address per window. Extra sends are silently dropped | Default |
| `EMAIL_SEND_WINDOW_SECONDS` | `900` | Window for the above | Default |
| `EMAIL_CODE_DAILY_MAX_FAILURES` | `15` | Wrong one-time codes per account and purpose over 24 h, across re-issued codes; beyond it the flow is blocked until the 24 h window passes | Default |
| `REAUTH_RATE_LIMIT` | `10` | Re-authentication requests (change password, delete account) per user per window | Default |
| `REAUTH_MAX_FAILURES` | `5` | Wrong current passwords per user (decaying) before re-auth is locked for the window | Default |
| `REAUTH_WINDOW_SECONDS` | `900` | Window / lock duration for the two above | Default |
| `AVATAR_UPLOAD_LIMIT` | `10` | Avatar / family-image uploads per user per window | Default |
| `AVATAR_UPLOAD_WINDOW_SECONDS` | `3600` | Window for the above | Default |
| `RECEIPT_UPLOAD_LIMIT` | `30` | Receipt attachment uploads per user per window (rejected files count too) | Default |
| `RECEIPT_UPLOAD_WINDOW_SECONDS` | `3600` | Window for the above | Default |
| `UPI_INITIATE_RATE_LIMIT` | `30` | New UPI payments per user per window (idempotent replays don't count) | Default |
| `UPI_INITIATE_RATE_WINDOW_SECONDS` | `600` | Window for the above | Default |
| `UPI_PARSE_RATE_LIMIT` | `120` | `POST /upi/parse` calls per user per window | Default |
| `UPI_PARSE_RATE_WINDOW_SECONDS` | `600` | Window for the above | Default |
| `EXPORT_RATE_LIMIT` | `20` | CSV exports (`/transactions/export.csv`, `/reports/export.csv`) per user per window | Default |
| `EXPORT_RATE_WINDOW_SECONDS` | `600` | Window for the above | Default |
| `ANALYTICS_RATE_LIMIT` | `300` | Requests to `/reports/*` and `/dashboard` per user per window | Default; raise if the app polls the dashboard aggressively |
| `ANALYTICS_RATE_WINDOW_SECONDS` | `300` | Window for the above | Default |
| `DEVICE_REGISTER_RATE_LIMIT` | `20` | `POST /notifications/devices` per user per window | Default |
| `DEVICE_REGISTER_RATE_WINDOW_SECONDS` | `3600` | Window for the above | Default |

All per-user limits answer **429** `rate_limited` with a `Retry-After` header.

### Email (SMTP)

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `SMTP_HOST` | `localhost` | SMTP server | Your provider's SMTP host |
| `SMTP_PORT` | `1025` | SMTP port (Mailpit) | `587` with STARTTLS or `465` with implicit TLS |
| `SMTP_USERNAME` | *(unset)* | SMTP auth user | Provider credential |
| `SMTP_PASSWORD` | *(unset)* | SMTP auth password (secret) | Keep it in a secret store |
| `SMTP_STARTTLS` | `false` | Upgrade with STARTTLS (port 587) | `true` for 587 |
| `SMTP_USE_TLS` | `false` | Implicit TLS (port 465) | `true` for 465. Don't set both |
| `EMAIL_FROM` | `Family Budget <no-reply@familybudget.local>` | From header | A domain you control with SPF/DKIM/DMARC set up |

Only the **worker** sends email. The API only enqueues jobs.

### Files

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `UPLOAD_DIR` | `./data/uploads` | Root directory for avatars, family images and receipts (relative paths resolve from the working directory) | `/data/uploads` with a persistent volume mounted there. The `prod` image creates `/data/uploads` owned by the non-root `app` user. The default relative path would resolve to `/app/data/uploads`, which that user can't create |
| `MAX_AVATAR_BYTES` | `2097152` (2 MiB) | Max avatar / family image upload size (413 above it) | Default |
| `MAX_RECEIPT_BYTES` | `5242880` (5 MiB) | Max receipt attachment size (1 KiB – 50 MiB; 413 above it). Read with a hard cap, never buffered beyond limit + 1 byte | Default. Also keep the proxy's body limit above it |

### Push notifications (Expo)

| Variable | Default | Meaning | Production guidance |
|---|---|---|---|
| `PUSH_ENABLED` | `true` | `false` = the worker's push sender marks pending pushes `skipped` (in-app notifications still work) | `true` once the app has an Expo project |
| `EXPO_PUSH_URL` | `https://exp.host/--/api/v2/push/send` | Expo push API endpoint | Leave as is |
| `EXPO_ACCESS_TOKEN` | *(unset)* | Bearer token, only needed if "enhanced push security" is enabled for the Expo project (secret) | Set it if you enable enhanced security; keep it in a secret store |
| `EXPO_PUSH_TIMEOUT_SECONDS` | `15.0` | HTTP timeout per Expo request (0–60) | Default |

## Variables outside `Settings`

| Variable | Used by | Meaning |
|---|---|---|
| `TEST_DATABASE_URL` | `backend/tests/conftest.py` | DB for pytest. Default `postgresql+asyncpg://postgres:postgres@localhost:5432/family_budget_test`. The database name **must contain `test`** or the suite refuses to run. The suite drops and recreates the schema and holds an advisory lock on the database for the whole run, so a second run against the same database **waits** (and says so). Use one database per concurrent run ([TESTING.md](TESTING.md)) |

## Docker Compose

Compose reads the repo-root `.env` twice:

1. For **variable substitution** in `docker-compose.yml` (the port variables below).
2. As `env_file` for `api` and `worker` (optional: the stack starts without it).

The `x-backend-env` block then **overrides** these values inside the `api` and `worker` containers,
because inside the compose network services are reached by name:

| Variable | Value inside containers |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget` |
| `TEST_DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget_test` |
| `SMTP_HOST` / `SMTP_PORT` | `mailpit` / `1025` |
| `UPLOAD_DIR` | `/data/uploads` (named volume `uploads`) |

So the `localhost` URLs in `.env` are for tools on your host (e.g. running pytest or Alembic
outside Docker). Everything else in `.env` (JWT secret, CORS, limits) passes through unchanged.
To point a one-off container at a different DB, pass `-e`:
`docker compose run --rm --no-deps -e DATABASE_MIGRATION_URL=... api alembic upgrade head`.

Host-port variables (set them in `.env` or your shell before `docker compose up`):

| Variable | Default | Binds |
|---|---|---|
| `POSTGRES_PORT` | `5432` | `127.0.0.1:<port>` → postgres 5432 |
| `API_PORT` | `8000` | `<all interfaces>:<port>` → api 8000. Reachable from phones on your LAN; allow it in the OS firewall |
| `MAILPIT_SMTP_PORT` | `1025` | `127.0.0.1:<port>` → mailpit SMTP |
| `MAILPIT_UI_PORT` | `8025` | `127.0.0.1:<port>` → Mailpit web UI |

If you change `POSTGRES_PORT`, also update the port in `.env`'s `DATABASE_URL`/`TEST_DATABASE_URL`
for host-side tools. Containers are unaffected. The postgres container's own credentials are
fixed in compose (`postgres`/`postgres`, DB `family_budget`) and are for local use only.

Shell syntax for one-off overrides:

```bash
# Git Bash
API_PORT=8080 docker compose up -d
```

```powershell
# PowerShell
$env:API_PORT = "8080"; docker compose up -d
```

## Mobile (`mobile/.env`)

| Variable | Default | Meaning |
|---|---|---|
| `EXPO_PUBLIC_API_URL` | Android: `http://10.0.2.2:8000`, others: `http://localhost:8000` (`src/constants/config.ts`) | API **origin** as seen from the device, without `/api/v1` (the app appends it). Trailing slashes are stripped |

Typical values (`mobile/.env.example`):

- Android emulator: `http://10.0.2.2:8000`
- iOS simulator or web: `http://localhost:8000`
- Physical phone on the same Wi-Fi: `http://<your-PC-LAN-IP>:8000` (allow the port in the firewall)
- Release builds: `https://api.your-domain`. Release builds must use HTTPS: Android blocks
  cleartext HTTP by default and iOS ATS does too.

`EXPO_PUBLIC_*` values are **inlined into the JS bundle at build time**. They are public. Never
put secrets there. Restart `npx expo start` (ideally with `-c`) after changing them. For EAS
builds, set the value per build profile (see [DEPLOYMENT.md](DEPLOYMENT.md#6-mobile-builds-eas)).

Code constants (not env-configurable): `REQUEST_TIMEOUT_MS = 15000` and
`APP_LOCK_GRACE_MS = 60000` (`src/constants/config.ts`).
