# Neon (staging and production PostgreSQL)

Local development uses the compose `postgres` container. For hosted environments the backend is
built to run on [Neon](https://neon.tech) serverless Postgres. Nothing in the code is
Neon-specific beyond URL handling, so any PostgreSQL 15+ works the same way.

## Layout

- **One Neon project** for the app, in the available region closest to your API host and users
  (check Neon's region list. For users in India that is typically `aws-ap-southeast-1`,
  Singapore). Put the API host in the same cloud region.
- **One branch per environment**:
  - `production` is the project's default (root) branch.
  - `staging` is branched from production, or kept separate if staging must never contain real
    user data. Recreate it from a sanitised source rather than from production in that case.
  - Short-lived branches (`pr-123`, `restore-drill-2026-11`) for migration rehearsals and restore
    drills. Delete them afterwards.
- One database per branch (e.g. `family_budget`) and two roles per branch (see
  [Roles](#least-privilege-roles)).

Each branch has its own compute endpoint, and Neon gives two hostnames per endpoint:

| Hostname | Kind | Use it for |
|---|---|---|
| `ep-xxxx-pooler.<region>.aws.neon.tech` | **Pooled** (PgBouncer, transaction mode) | `DATABASE_URL`: API and worker |
| `ep-xxxx.<region>.aws.neon.tech` | **Direct** | `DATABASE_MIGRATION_URL`: Alembic only |

## Connection settings

```dotenv
ENVIRONMENT=production
# Runtime: pooled host, least-privilege app role
DATABASE_URL=postgresql://fb_app:<password>@ep-xxxx-pooler.ap-southeast-1.aws.neon.tech/family_budget?sslmode=require
DB_PGBOUNCER=true
# Migrations: direct host, owner/migration role
DATABASE_MIGRATION_URL=postgresql://fb_owner:<password>@ep-xxxx.ap-southeast-1.aws.neon.tech/family_budget?sslmode=require
```

You can paste the connection string exactly as Neon shows it
(`postgresql://...?sslmode=require&channel_binding=require`). `normalize_async_db_url` in
`backend/app/core/config.py` turns it into something asyncpg understands:

- The scheme `postgres://` or `postgresql://` (or `+psycopg`/`+psycopg2`) becomes
  `postgresql+asyncpg://`.
- `sslmode` (or `ssl`) is **removed from the URL** and passed to asyncpg as the `ssl` connect
  argument (`require`, `verify-full`, and so on). `sslmode=disable` means no TLS.
- `channel_binding` is removed. asyncpg doesn't accept it. SCRAM authentication is still used.

The test `test_neon_url_normalisation` (`backend/tests/test_platform.py`) covers this.

TLS notes:

- `sslmode=require` encrypts the connection but, like libpq, **does not verify the server
  certificate**. For production prefer `sslmode=verify-full`. asyncpg then verifies the
  certificate and hostname against the system CA bundle (present in the `python:3.12-slim`
  base image). Check it once against your endpoint before relying on it.
- URL-encode special characters in passwords (`@` → `%40`, `/` → `%2F`, `#` → `%23`).

Why `DB_PGBOUNCER=true` with the pooler: PgBouncer in transaction mode can hand each transaction
a different server connection, so asyncpg's per-connection prepared-statement cache breaks. The
flag sets `statement_cache_size=0` and gives each prepared statement a unique name
(`backend/app/db/session.py`). Everything the app relies on still works through the pooler:
`SELECT ... FOR UPDATE SKIP LOCKED`, `INSERT ... ON CONFLICT` and row locks all live inside a
single transaction. The app uses no session-level state, advisory locks or `LISTEN/NOTIFY`.

Why the direct URL for Alembic: migrations are long-lived DDL sessions and should not go
through a transaction pooler. `alembic/env.py` uses `DATABASE_MIGRATION_URL` (falling back to
`DATABASE_URL`) and always forces `db_pgbouncer=False`.

Pool sizing: the pooler accepts many client connections, so the app-side pool
(`DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=10` per process) is rarely the bottleneck. Keep
`pool × uvicorn workers × replicas` well under the pooler's client limit for your plan.

## Least-privilege roles

Use two roles per branch:

| Role | Used by | Privileges |
|---|---|---|
| `fb_owner` (or Neon's default `<db>_owner`) | Alembic (`DATABASE_MIGRATION_URL`), humans doing emergency maintenance | Owns the schema and tables. DDL allowed |
| `fb_app` | API and worker (`DATABASE_URL`) | DML only on existing tables: `SELECT, INSERT, UPDATE, DELETE`. No DDL, no `TRUNCATE`, doesn't own anything |

Create the database and owner in the Neon console (or `neonctl`). Roles created in the console
get a generated password. Then, connected **as the owner role** to the target database (e.g.
through the Neon SQL editor or `psql "<direct owner URL>"`):

```sql
-- 1. App role (Neon requires a strong password for roles created via SQL)
CREATE ROLE fb_app WITH LOGIN PASSWORD '<generate a long random password>';

-- 2. Connect + schema usage, but no CREATE in the schema
GRANT CONNECT ON DATABASE family_budget TO fb_app;
GRANT USAGE ON SCHEMA public TO fb_app;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;   -- already the default on PG 15+

-- 3. DML on everything that exists now
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO fb_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO fb_app;  -- none today (UUID PKs); future-proof

-- 4. ...and on everything future migrations create (run as the role that runs Alembic)
ALTER DEFAULT PRIVILEGES FOR ROLE fb_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO fb_app;
ALTER DEFAULT PRIVILEGES FOR ROLE fb_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO fb_app;

-- 5. Optional: the app has no business changing the migration marker
REVOKE INSERT, UPDATE, DELETE ON alembic_version FROM fb_app;
```

Replace `fb_owner` with the actual name of the role that runs Alembic. `ALTER DEFAULT
PRIVILEGES` only covers objects created **by that role**. Check the result:

```sql
SELECT grantee, table_name, string_agg(privilege_type, ', ')
FROM information_schema.role_table_grants
WHERE grantee = 'fb_app' GROUP BY 1, 2 ORDER BY 2;
```

Run steps 1–4 on a fresh branch **after** the first `alembic upgrade head`, or before it if
you rely on the default privileges in step 4. Branches created from a configured branch inherit
the roles and grants.

The app needs `DELETE`: housekeeping purges expired sessions, tokens, counters and jobs, and a
successful login resets its failure counter. It does not need `TRUNCATE` (only the test suite
uses that, on the local test DB).

## Running migrations against Neon

Run them as a release step before starting new API/worker versions, using the **direct** URL and
the **owner** role. See [MIGRATIONS.md](MIGRATIONS.md) for the general workflow.

With the production image (recommended, same code as what you deploy):

```bash
docker run --rm \
  -e DATABASE_URL="<pooled app URL>" \
  -e DATABASE_MIGRATION_URL="<direct owner URL>" \
  -e JWT_SECRET_KEY="<the environment's real secret>" \
  -e ENVIRONMENT=production \
  family-budget-api:<tag> alembic upgrade head
```

Settings are validated even for Alembic. `JWT_SECRET_KEY` must be present (and not a placeholder
when `ENVIRONMENT` is staging/production), even though migrations don't use it. Alternatively
pass everything with `--env-file prod.env`.

From your dev machine using the compose dev image (Git Bash, from the repo root):

```bash
docker compose run --rm --no-deps \
  -e DATABASE_MIGRATION_URL="postgresql://fb_owner:<pw>@ep-xxxx.ap-southeast-1.aws.neon.tech/family_budget?sslmode=require" \
  api alembic upgrade head
```

PowerShell: same command on one line, or use a backtick (`` ` ``) instead of `\` for line
continuation. Keep the URL in double quotes because it contains `?` and `&`.

Here `DATABASE_URL` stays the local compose value, which is fine because Alembic prefers
`DATABASE_MIGRATION_URL`. Always confirm the target before upgrading:

```bash
docker compose run --rm --no-deps -e DATABASE_MIGRATION_URL="<direct URL>" api alembic current
```

**Rehearse risky migrations on a branch first.** Create a branch from production (Console →
Branches → New branch, or `neonctl branches create --name mig-rehearsal --parent production`),
run `alembic upgrade head` against that branch's direct URL, smoke-test, then delete the branch.

## Scale to zero

Neon suspends an idle compute after a few minutes, depending on plan and settings. This app keeps
it busy:

- The **worker polls `background_jobs` every `WORKER_POLL_INTERVAL_SECONDS` (2 s)**, so the
  compute **never suspends while a worker is running**. That's correct for production because
  emails go out within seconds. For a low-traffic staging branch you can stop the worker when it
  isn't needed, or accept the compute hours.
- Health checks against `/health/ready` also run a query. Point frequent uptime checks at
  `/health` (no DB) instead.
- After a suspend, the first connection takes extra time to wake the compute.
  `pool_pre_ping=True` handles connections that were dropped while it slept.

## Backups and restore

See [BACKUP_RESTORE.md](BACKUP_RESTORE.md#neon): point-in-time restore, branching for drills,
and `pg_dump` for off-platform copies.
