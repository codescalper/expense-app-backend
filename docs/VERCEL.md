# Deploying the backend to Vercel (+ Neon)

The FastAPI backend can run as a single **Vercel Function** (Python runtime, Fluid compute) on
top of **Neon Postgres**, with files in a **private S3-compatible bucket** (Neon object storage)
or a private Vercel Blob store. Everything Vercel-specific is opt-in through settings, so the
docker-compose / self-hosted path ([DEPLOYMENT.md](DEPLOYMENT.md)) is unchanged.

Related: [NEON.md](NEON.md) (database, roles) · [ENVIRONMENT.md](ENVIRONMENT.md) (all settings)
· [MIGRATIONS.md](MIGRATIONS.md)

## How the app maps onto Vercel

| Self-hosted piece | On Vercel | Setting / file |
|---|---|---|
| `uvicorn app.main:app` | One Vercel Function; Vercel finds `app` through `[tool.vercel] entrypoint = "app.main:app"` in `backend/pyproject.toml` | `backend/pyproject.toml`, `backend/vercel.json` |
| `requirements.txt` | Vercel installs **`pyproject.toml`** when it exists (and ignores `requirements.txt`), so `[project].dependencies` mirrors `requirements.in` at the pinned versions and `[tool.uv].constraint-dependencies` pins every transitive package. `tests/test_vercel.py` fails if they drift. Python is pinned to 3.12 (`requires-python = ">=3.12,<3.13"`) | `backend/pyproject.toml` |
| SQLAlchemy pool (5 + 10 per process) | No app-side pool (`NullPool`): instances are frozen/replaced between invocations. Neon's pooler does the pooling. Nothing connects at import | `SERVERLESS=true`, `DB_PGBOUNCER=true`, pooled `DATABASE_URL` |
| Worker sends emails from `background_jobs` | The request still writes the job in its own transaction (outbox), then a FastAPI background task delivers *that* job right after the response is sent, through the normal worker code path (expiry check, payload wipe, retry with backoff). Unknown addresses still get a `noop_email` job, so response time doesn't reveal whether an address exists | `EMAIL_DELIVERY=inline` |
| Worker periodic tasks + housekeeping | `GET/POST /internal/cron/tick` runs one bounded pass: due jobs (retries), every periodic task whose interval has elapsed (`budget_alerts` 10 min, `unusual_spending` 1 h, `push_sender` 10 s, `recurring` 15 min, `upi_pending_reminders` 5 min, `housekeeping` 30 min), then due jobs again. Last-run times live in `periodic_task_runs` (migration 0004); a transaction-scoped advisory lock stops overlapping ticks; the pass stops starting new work after `CRON_TIME_BUDGET_SECONDS` | `CRON_SECRET`, `backend/vercel.json` `crons` |
| `UPLOAD_DIR` volume | Private bucket, SigV4-signed, path-style. Objects are never public; avatars, family images and receipts are streamed back through the same authenticated endpoints | `STORAGE_BACKEND=s3` + `S3_*` (or `vercel_blob` + `BLOB_READ_WRITE_TOKEN`) |
| `--proxy-headers` + `FORWARDED_ALLOW_IPS` | No uvicorn. The client IP comes from Vercel's own `x-vercel-forwarded-for` / `x-real-ip` / `x-forwarded-for` (Vercel overwrites them) | `TRUST_VERCEL_PROXY=true` |
| `alembic upgrade head` release job | Run from your machine against Neon's **direct** URL before deploying | `backend/scripts/migrate_remote.sh` / `.ps1` |

Start-up validation: with `ENVIRONMENT=production` (or `staging`) **on Vercel** (`VERCEL=1`,
set by Vercel itself) the app refuses to start unless `SERVERLESS=true`,
`EMAIL_DELIVERY=inline`, `STORAGE_BACKEND=s3|vercel_blob` and `CRON_SECRET` are set. Softer
problems are logged once at start-up as `config_warning` lines (e.g. `TRUST_VERCEL_PROXY` off,
`MAX_RECEIPT_BYTES` above Vercel's 4.5 MB body limit).

## Environment variables

Add these to the Vercel project for the **Production** environment. Keep a copy in the
git-ignored repo-root `.env.vercel` (template: [`/.env.vercel.example`](../.env.vercel.example)).

| Name | Secret | Value |
|---|---|---|
| `ENVIRONMENT` | no | `production` |
| `SERVERLESS` | no | `true` |
| `EMAIL_DELIVERY` | no | `inline` |
| `STORAGE_BACKEND` | no | `s3` (or `vercel_blob`) |
| `TRUST_VERCEL_PROXY` | no | `true` |
| `ENABLE_DOCS` | no | `false` (docs are forced off in production anyway) |
| `CORS_ORIGINS` | no | empty (native app) or an explicit list |
| `DATABASE_URL` | **yes** | Neon **pooled** (`-pooler`) URL, app role, `?sslmode=require` |
| `DB_PGBOUNCER` | no | `true` |
| `JWT_SECRET_KEY` | **yes** | `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `CRON_SECRET` | **yes** | letters/digits only, 16+ chars: `python -c "import secrets; print(secrets.token_hex(32))"` |
| `CRON_TIME_BUDGET_SECONDS` | no | `50` (keep below `maxDuration` 60; `25` if an external pinger times out at 30 s) |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_STARTTLS`, `SMTP_USE_TLS`, `SMTP_USERNAME`, `EMAIL_FROM` | no | your SMTP provider (587 + STARTTLS or 465 + TLS) |
| `SMTP_PASSWORD` | **yes** | SMTP password / app password |
| `S3_ENDPOINT_URL` | no | e.g. `https://<id>.storage.<cell>.us-east-2.aws.neon.tech` (must be `https://`) |
| `S3_REGION` | no | e.g. `us-east-2` |
| `S3_BUCKET` | no | the private bucket name |
| `S3_ACCESS_KEY_ID` | **yes** | object-storage access key |
| `S3_SECRET_ACCESS_KEY` | **yes** | object-storage secret key |
| `BLOB_READ_WRITE_TOKEN` | **yes** | only with `STORAGE_BACKEND=vercel_blob`: read-write token of a **private** Blob store |
| `MAX_RECEIPT_BYTES` | no | `4194304` (Vercel rejects bodies over 4.5 MB) |
| `PUSH_ENABLED` | no | `true` |
| `EXPO_ACCESS_TOKEN` | **yes** | only if Expo "enhanced push security" is on |

Do **not** add `DATABASE_MIGRATION_URL` (owner role, DDL rights) to Vercel: it is only used by
the migration script on your machine. Never set `VERCEL` yourself, and don't set
`FORWARDED_ALLOW_IPS` (uvicorn isn't used on Vercel).

## Deploy, step by step

All commands run from the **repository root** unless noted; the Vercel CLI must be 48.1.8 or
newer (`npx vercel@latest ...` always is).

### 1. Link the project (Root Directory = `backend`)

```bash
npx vercel@latest login
cd backend
npx vercel@latest link --yes --project family-budget-api   # creates/links the project, writes backend/.vercel/
```

Linking from `backend/` makes it the deployment root, so only `backend/` is uploaded
(`backend/.vercelignore` keeps tests, data and env files out). If you later connect the Git
repository instead, set **Project Settings → Build and Deployment → Root Directory** to
`backend` (the repo-root `.vercelignore` keeps `.env*`, `mobile/` etc. out of uploads).

Optional but recommended: put the function next to the database. Neon `us-east-2` is
AWS Ohio → Vercel region `cle1` (Project Settings → Functions → Function Region, or
`"regions": ["cle1"]` in `backend/vercel.json`). The default is `iad1` (Washington, D.C.).

### 2. Add the environment variables

From `backend/` (linked), push every variable in `../.env.vercel` except the migration URL.
Values are piped from the file, so they never appear in shell history or on a command line:

```bash
# Git Bash, from backend/
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  name="${line%%=*}"; value="${line#*=}"; value="${value%$'\r'}"
  [ -z "$value" ] && continue
  [ "$name" = "DATABASE_MIGRATION_URL" ] && continue
  case "$name" in
    DATABASE_URL|JWT_SECRET_KEY|CRON_SECRET|SMTP_PASSWORD|S3_ACCESS_KEY_ID|S3_SECRET_ACCESS_KEY|BLOB_READ_WRITE_TOKEN|EXPO_ACCESS_TOKEN)
      flag=--sensitive ;;
    *) flag=--no-sensitive ;;
  esac
  printf '%s' "$value" | npx vercel@latest env add "$name" --environment production $flag --force --yes
done < ../.env.vercel
npx vercel@latest env ls production
```

(Or add them in the dashboard: Project → Settings → Environment Variables, environment
"Production", marking the secret ones as Sensitive.)

### 3. Run the migrations against Neon

```bash
backend/scripts/migrate_remote.sh            # Git Bash / Linux / macOS
# PowerShell:  backend\scripts\migrate_remote.ps1
```

The script reads only `DATABASE_MIGRATION_URL` from `.env.vercel` (or the environment), prints
the target host without the password, runs `alembic current`, `alembic upgrade head`, then
`alembic current` again inside the compose `api` image (`docker compose` must work; no local
Python needed). Other Alembic commands pass through: `migrate_remote.sh current`,
`migrate_remote.sh downgrade -1`. Run it **before** every deploy that adds a migration
(expand/contract rules: [MIGRATIONS.md](MIGRATIONS.md)). Then grant the app role access to any
new tables if you rely on explicit grants ([NEON.md](NEON.md#least-privilege-roles)).

### 4. Deploy

```bash
cd backend
npx vercel@latest deploy --prod
```

The build installs the dependencies from `pyproject.toml` with uv and deploys `app/main.py:app`
as one function (`maxDuration` 60 s, tests/alembic/scripts excluded from the bundle).

### 5. Background work (cron)

`backend/vercel.json` ships a **daily** cron (`17 3 * * *` UTC → `GET /internal/cron/tick`),
which works on every plan. Vercel sends `Authorization: Bearer $CRON_SECRET` automatically;
the endpoint returns 401 for anything else and 404 when `CRON_SECRET` is unset.

- **Hobby** (cron at most once a day, fired anywhere within the hour): keep the daily entry
  and add an external pinger for the frequent work. Example with cron-job.org: create a job
  for `https://<your-domain>/internal/cron/tick`, schedule *every 5 minutes*, method `POST`
  (or `GET`), header `Authorization: Bearer <CRON_SECRET>`, request timeout as high as allowed
  (if it is 30 s, set `CRON_TIME_BUDGET_SECONDS=25`). Overlapping or duplicate calls are
  harmless (advisory lock + idempotent tasks); a locked tick answers `{"status": "locked"}`.
- **Pro** (per-minute cron allowed): change the schedule in `backend/vercel.json` to
  `"*/5 * * * *"` and redeploy; no external pinger needed.

Emails don't depend on the cron (inline delivery); the tick only retries failed sends. Push
notifications, budget alerts, recurring bills, UPI reminders and housekeeping run on ticks, so
with a daily-only cron they run once a day.

Rotating `CRON_SECRET`: update it, redeploy (a deployment keeps the value it was built with),
then update the pinger's header.

### 6. Verify

```bash
BASE=https://<your-production-domain>
curl -s $BASE/health                                   # {"status":"ok"}
curl -s $BASE/health/ready                             # {"status":"ok","database":"up"}
curl -s -o /dev/null -w '%{http_code}\n' $BASE/docs    # 404 in production
curl -s -o /dev/null -w '%{http_code}\n' $BASE/internal/cron/tick            # 401
curl -s -H "Authorization: Bearer $CRON_SECRET" $BASE/internal/cron/tick     # {"status":"ok",...}
curl -s -X POST -H "Authorization: Bearer $CRON_SECRET" $BASE/internal/cron/tick  # "ran": [] right after
# Registration sends the verification email (check the inbox; the response is always 202):
curl -s -X POST $BASE/api/v1/auth/register -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"a long passphrase here","full_name":"You"}'
```

Then, signed in from the app: upload an avatar and a receipt and download them again; check
`GET /api/v1/users/me/sessions` shows your real public IP (proves `TRUST_VERCEL_PROXY`). Logs:
`npx vercel@latest logs <deployment-url>` or the dashboard; look for `config_warning` lines.

Timing check (anti-enumeration): `forgot-password` for a registered and an unregistered
address should take about the same time. Inline delivery relies on the response being sent
before the background task's SMTP conversation, which is how Starlette background tasks
behave on Vercel's streaming Python runtime. If you ever see the registered address
consistently slower by the SMTP round trip, switch to `EMAIL_DELIVERY=queue` with a 5-minute
tick (codes are valid for 30 minutes).

## Limitations and trade-offs

- **Function duration**: `maxDuration` is 60 s in `vercel.json` (Hobby allows up to 300 s,
  Pro 800 s). Ordinary requests are far below it; the cron tick budgets itself
  (`CRON_TIME_BUDGET_SECONDS`, default 50 s) and leaves the rest to the next tick. A job
  interrupted by a timeout stays `running` until housekeeping requeues it (10 min).
- **Cold starts**: a new instance imports FastAPI, SQLAlchemy, Pillow and argon2 (typically
  1–2 s), and a suspended Neon compute adds its own wake-up time to the first query. Fluid
  compute reuses warm instances, so this hits the first request after idle periods.
- **No connection pool** in the app: each request opens one connection to Neon's pooler (a
  few ms in-region, more cross-region: deploy the function in the database's region).
- **Cron frequency on Hobby**: once a day, anywhere within the scheduled hour. Use an
  external pinger for 5-minute work (above). Vercel Cron only runs on production deployments
  and does not retry failed invocations.
- **Request/response size**: Vercel rejects request bodies over 4.5 MB (keep
  `MAX_RECEIPT_BYTES=4194304`); file downloads are streamed, so they are not limited by the
  4.5 MB buffered-response cap.
- **Argon2 memory**: each login uses `ARGON2_MEMORY_KIB` (64 MiB) × at most
  `PASSWORD_HASH_CONCURRENCY` (2) per instance; Vercel's 2 GB default is plenty.
- **Preview deployments** use Preview-scoped env vars (none by default) and get Vercel
  Deployment Protection; point them at a Neon branch and a separate bucket if you use them.
- **Object storage**: objects are private and immutable (random keys); replaced or deleted
  avatars/images are removed after the DB commit (best effort; a failed delete only leaves an
  orphan). Soft-deleted receipts are kept, as with local storage. Back up the bucket as part
  of [BACKUP_RESTORE.md](BACKUP_RESTORE.md).
