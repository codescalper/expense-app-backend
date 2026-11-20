# Database migrations (Alembic)

- Config: `backend/alembic.ini`. The DB URL is **not** stored there.
- Environment: `backend/alembic/env.py`. It is async and imports `app.models` so
  `Base.metadata` is complete. It uses `compare_type=True`.
- URL resolution: `DATABASE_MIGRATION_URL`, then `DATABASE_URL`. The test suite injects its
  own URL via `config.attributes["database_url"]`. The PgBouncer flag is always off for
  migrations.
- Revisions: `backend/alembic/versions/`. File names follow `YYYYMMDD_<rev>_<slug>.py`. New
  revision files are auto-formatted with ruff (post-write hook).
- Current head: **`0003`**. Revisions:

  | Rev | File | What it does |
  |---|---|---|
  | `0001` | `20261120_0001_auth_foundation.py` | Auth & platform: `users`, `user_sessions`, `refresh_tokens`, `user_tokens`, `audit_logs`, `rate_limit_counters`, `background_jobs`; enum types `job_status`, `token_purpose`. |
  | `0002` | `20261120_0002_finance_schema.py` | Everything for Phases 3–7: families / members / invitations, accounts, transaction categories (**and the 26 built-in categories — 20 expense, 6 income — as seed rows with fixed UUIDs**), payees, transactions / tags / attachments, budgets / budget categories, recurring transactions, savings goals / contributions, UPI payment attempts, notifications / preferences / device tokens. Creates 18 shared enum types up front (`create_type=False` on the columns) and drops them explicitly in `downgrade()`. Handles the `transactions` ↔ `upi_payment_attempts` FK cycle with a separately added FK. Partial unique indexes use `NULLS NOT DISTINCT` (PostgreSQL 15+). |
  | `0003` | `20261120_0003_adjustment_sign.py` | Adds `transactions.adjustment_sign` (+1 raises / −1 lowers an account balance), backfills existing adjustments to +1 and adds `ck_transactions_adjustment_sign` (`type='adjustment'` ⇔ sign set, sign ∈ {−1, 1}). |

  See [ER.md](ER.md) for the resulting schema.

## Running commands

### In Docker (recommended locally)

The `api` container runs `alembic upgrade head` automatically every time it starts in the
compose dev stack. Manual commands, from the repo root:

```bash
docker compose exec api alembic current            # revision the DB is at
docker compose exec api alembic history --verbose  # all revisions
docker compose exec api alembic heads              # should print exactly one head
docker compose exec api alembic check              # fails if models differ from migrations
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade -1       # one step back
docker compose exec api alembic upgrade head --sql # print SQL without running it (offline mode)
```

`exec` needs the stack running. Use `docker compose run --rm --no-deps api alembic ...` when the
`api` container is down (postgres must still be up: `docker compose up -d postgres`).

These commands are identical in Git Bash and PowerShell. Git Bash only: if an argument is an
absolute container path (starts with `/`), prefix the command with `MSYS_NO_PATHCONV=1` so Git
Bash doesn't rewrite it into a Windows path.

### On the host (without Docker)

```bash
cd backend
python -m venv .venv
source .venv/Scripts/activate        # Git Bash on Windows; Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
alembic current                      # uses DATABASE_URL from ../.env (localhost:5432)
```

PowerShell: activate with `.\.venv\Scripts\Activate.ps1`.

## Creating a revision

1. Change or add models in `backend/app/models/`. Export new models from
   `app/models/__init__.py`, otherwise autogenerate won't see them. Use the conventions in
   `app/db/base.py`: `UUIDPkMixin`, `TimestampMixin`, `Money` for amounts, and timezone-aware
   datetimes.
2. Make sure your local DB is at head (`alembic current`).
3. Generate the revision. Pass `--rev-id` to keep the sequential numbering (next: `0004`):

   ```bash
   docker compose exec api alembic revision --autogenerate --rev-id 0004 -m "short description"
   ```

   The source is bind-mounted, so the file appears in `backend/alembic/versions/` on your host.
4. **Review the generated file. Autogenerate is a draft.** Check:
   - **Enum types.** `sa.Enum(...)` columns create a PostgreSQL type in `upgrade()`, but
     `drop_table` in `downgrade()` does **not** drop it. Add an explicit drop after the tables,
     as revision `0001` does:

     ```python
     sa.Enum(name="token_purpose").drop(op.get_bind(), checkfirst=True)
     sa.Enum(name="job_status").drop(op.get_bind(), checkfirst=True)
     ```

     Adding a value to an existing enum needs a hand-written
     `op.execute("ALTER TYPE ... ADD VALUE ...")`. Autogenerate doesn't detect it.
   - **Renames** show up as drop + add, which loses data. Rewrite them as
     `op.alter_column(..., new_column_name=...)` or `op.rename_table`.
   - **Server defaults, partial indexes (`postgresql_where`), CHECK constraints** come through
     with the expected names (the naming convention makes them deterministic). Compare them
     with the model.
   - **New NOT NULL columns on existing tables** need a `server_default` or a three-step
     migration: add nullable, backfill, then set NOT NULL.
   - **Data migrations** (backfills) use `op.execute` / `sa.table()` constructs, not ORM model
     imports. Models change over time and old revisions must keep working.
   - **Large tables** (e.g. `transactions`, `notifications`, `audit_logs`): create indexes with
     `postgresql_concurrently=True` inside `with op.get_context().autocommit_block():`.
   - `downgrade()` must exactly reverse `upgrade()`.
5. Round-trip it locally:

   ```bash
   docker compose exec api alembic upgrade head
   docker compose exec api alembic downgrade -1
   docker compose exec api alembic upgrade head
   docker compose exec api alembic check
   ```

6. Run the test suite (below). It also proves the full chain from base round-trips.
7. Commit the model change and the revision together.

## How the tests exercise migrations

`backend/tests/conftest.py` has a session-scoped fixture that runs, against
`TEST_DATABASE_URL`:

```python
command.downgrade(cfg, "base")
command.upgrade(cfg, "head")
```

So every test run executes **every `downgrade()` back to base, then every `upgrade()` to
head** on a real PostgreSQL. The whole suite then runs against the schema that the migrations
produced (not `metadata.create_all`); the built-in categories seeded by `0002` are re-inserted
after each per-test `TRUNCATE` from the migration's own `DEFAULT_CATEGORIES` list. A broken downgrade, such as a leftover enum type that makes
the next `CREATE TYPE` fail, fails the suite. CI additionally runs `alembic upgrade head` on
an empty database followed by `alembic check` (`.github/workflows/ci.yml`).

Verified round trip (integration pass, scratch database):

```bash
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE family_budget_scratch"
U=postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget_scratch
docker compose run --rm --no-deps -e DATABASE_URL=$U -e DATABASE_MIGRATION_URL=$U api sh -c \
  "alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head && alembic check"
# → 0003 (head); "No new upgrade operations detected." (twice)
```

## Upgrade, downgrade and rollback strategy

- **Forward-only in production by default.** Deploy order is:
  1. Build the image.
  2. `alembic upgrade head` (release step, direct DB URL, owner role).
  3. Roll out the API and worker.

  See [DEPLOYMENT.md](DEPLOYMENT.md).
- **Write expand/contract migrations** so the previous app version keeps working on the new
  schema. If the new code is broken you can then roll back the **code** without touching the
  schema:
  - *Expand* (release N): add nullable columns or new tables, and dual-write.
  - *Contract* (release N+1 or later): drop old columns once no running version reads them.
- **Schema rollback** (`alembic downgrade <rev>`) is for staging, or for a migration that has
  just failed or is clearly wrong and has written no data worth keeping. Downgrades that drop
  columns or tables destroy data, so take a backup or Neon branch first
  ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)).
- **Serious data incident**: restore instead of downgrading. Use Neon point-in-time restore to
  just before the release ([BACKUP_RESTORE.md](BACKUP_RESTORE.md#neon)).
- PostgreSQL DDL is transactional and `env.py` runs each migration run in a transaction, so a
  failing upgrade leaves the schema unchanged. Exceptions are statements that can't run in a
  transaction, such as `CREATE INDEX CONCURRENTLY`.
- Only one process should run migrations at a time. Run them as a single release job, not on
  every container start. The dev compose `api` command does run them on start. That is a
  local convenience only, and the `prod` image doesn't do it.

## Rules

- **Never edit a production schema by hand** (console, `psql`, GUI). Every change goes through
  a reviewed Alembic revision, or the next autogenerate will try to "fix" the drift. If an
  emergency hotfix was applied manually, capture it in a revision immediately and verify with
  `alembic check`.
- **Never edit a revision that has already run** in any shared environment. Write a new one.
- Keep a single head. If two branches both add revisions, merge with
  `alembic merge -m "merge" <rev1> <rev2>` (or re-parent the newer one before merging the PRs).
- Don't `stamp` a production database unless you've verified the schema matches that revision.
