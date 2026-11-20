# Testing

| Suite | Count | Runner | Location |
|---|---|---|---|
| Backend | **418 tests** in 24 files (per-file counts below; verified 2026-11-20) | pytest + pytest-asyncio + httpx (ASGI transport) | `backend/tests/` |
| Mobile | see [PROGRESS.md](../PROGRESS.md) (mobile work in progress; counts change often) | Jest (`jest-expo`) + React Native Testing Library | `mobile/__tests__/` |

CI (`.github/workflows/ci.yml`) runs all of the following on every push to `main` and every PR.

## Backend

Backend tests run against a **real PostgreSQL** database, not mocks or SQLite. The schema is
built by the real Alembic migrations.

### Run in Docker (recommended)

From the repo root:

```bash
docker compose up -d postgres                     # --no-deps below won't start it for you

docker compose run --rm --no-deps api pytest      # all 418 tests (~6-7 min)
docker compose run --rm --no-deps api pytest -q tests/test_auth.py -k refresh   # a subset
docker compose run --rm --no-deps api ruff check .
docker compose run --rm --no-deps api ruff format --check .
docker compose run --rm --no-deps api mypy app    # strict mode
docker compose run --rm --no-deps -e HOME=/tmp api pip-audit -r requirements.txt
```

- `run --rm --no-deps api <cmd>` starts a throwaway container from the `dev` image (which has
  pytest, ruff, mypy and pip-audit) with the source bind-mounted, and replaces the normal
  migrate-and-serve command.
- The container sees `TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget_test`
  (compose override).

### One test database per concurrent run

A run drops and re-creates the schema at start-up and truncates every table around every test,
and several tests exercise worker code that scans whole tables (UPI reminders, the push sender,
notification checks). Two runs sharing one database therefore break each other in ways that
look like flaky tests (they pass alone). The suite now holds a PostgreSQL **advisory lock** on
its database for the whole run: a second run against the same database prints
`another test run is using <db>; waiting for it…` and waits. To run in parallel (e.g. several
contributors or agents), give each run its own database:

```bash
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE family_budget_test_<you>"
docker compose run --rm --no-deps \
  -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget_test_<you> \
  api pytest -q
```
- **`HOME=/tmp` for pip-audit:** the container user's home is `/app`, which is your bind-mounted
  `backend/` folder. Without the override pip-audit writes its cache into your working tree.
- **Git Bash:** prefix the pip-audit command with `MSYS_NO_PATHCONV=1`, otherwise Git Bash
  rewrites `HOME=/tmp` into a Windows path (`C:/Users/<you>/AppData/Local/Temp`). pip-audit then
  creates a literal `backend/C:/Users/...` directory tree in the repo. If you see that folder,
  delete it.

  ```bash
  MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -e HOME=/tmp api pip-audit -r requirements.txt
  ```

  PowerShell doesn't rewrite paths, so the plain command works there.

**Test database missing?** `infra/postgres/init.sql` creates `family_budget_test` only when the
`pgdata` volume is first initialised. If your volume predates that file:

```bash
docker compose exec postgres createdb -U postgres family_budget_test
```

### Run on the host

```bash
cd backend
python -m venv .venv && source .venv/Scripts/activate   # PowerShell: .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt -r requirements-dev.txt
docker compose up -d postgres                           # (from the repo root) DB on localhost:5432
pytest
```

`conftest.py` reads `TEST_DATABASE_URL` from the **process environment** (not from `.env`).
The default is `postgresql+asyncpg://postgres:postgres@localhost:5432/family_budget_test`. If you
changed `POSTGRES_PORT`, export a matching URL first (`export TEST_DATABASE_URL=...` in Git Bash,
`$env:TEST_DATABASE_URL = "..."` in PowerShell).

### How the fixtures work (`backend/tests/conftest.py`)

- **Safety guard:** the suite refuses to run unless the database name contains `test`.
- Environment forced before import: `ENVIRONMENT=test` (NullPool), cheap Argon2 parameters,
  `RATE_LIMIT_ENABLED=true`, a temp `UPLOAD_DIR`, `CORS_ORIGINS=http://localhost:8081`.
- **Session fixture: `alembic downgrade base` then `alembic upgrade head`.** Every run proves the
  whole migration chain round-trips ([MIGRATIONS.md](MIGRATIONS.md#how-the-tests-exercise-migrations)).
- **Session lock:** an advisory lock on the test database is held for the whole run (see above).
- Each test starts **and ends** with `TRUNCATE` of every mapped table (taken from
  `Base.metadata`, so new tables are covered automatically); the built-in categories seeded by
  migration `0002` are re-inserted from the migration's own list. Per-test settings tweaks made
  through the `settings` fixture are restored afterwards.
- The HTTP client is `httpx.AsyncClient` over `ASGITransport`, with client IP `203.0.113.10`.
- Emails are asserted through the queued `send_email` job payloads (`emails_to`,
  `latest_code`). No SMTP is needed.

### What the backend suites cover

| File | Tests | Covers |
|---|---|---|
| `test_auth.py` | 37 | Registration / verification codes, anti-enumeration, login throttling and email-keyed lockout, per-IP limits, JWT validation, refresh rotation and reuse detection, logout(-all), password reset, email quota, absolute session lifetime |
| `test_platform.py` | 24 | Health, security headers, CORS, body caps, config validation, Argon2, password policy, log redaction, worker (retries, dedupe, payload wiping, housekeeping), hashing concurrency |
| `test_users.py` | 14 | Profile, change password, sessions (incl. IDOR), avatar validation, re-auth limits, account deletion |
| `test_families.py` | 17 | Families, invitations (single-use, expiry, email-bound), roles, ownership transfer, leave/remove, cross-family isolation |
| `test_permissions.py` | 11 | Permission matrix, `ScopeFilter`, `require_record_write`, 404-vs-403 rules |
| `test_accounts.py` | 5 | Accounts CRUD, balances, archive, scope/role rules |
| `test_categories.py` | 12 | Built-ins read-only, sub-categories, unique names, archive, delete with reassign |
| `test_payees.py` | 21 | Payees, VPA uniqueness, recent/frequent, category suggestion |
| `test_ledger.py` | 4 | Accounting rules (transfers, refunds, adjustments, pending) |
| `test_transactions.py` | 15 | CRUD, validation codes, idempotency, filters, summary, CSV export, restore, internal API |
| `test_attachments.py` | 6 | Receipt validation (images re-encoded, unsafe PDFs rejected), limits, permissions |
| `test_upi_uri.py` | 85 | UPI URI parsing/building, sanitising return data (parametrised) |
| `test_upi.py` | 22 | UPI initiate/launch/return/confirm/cancel, idempotency and races, reminders, permissions |
| `test_budgets.py` | 21 | Period math, spending rules, carry-forward, alerts (once per period), permissions |
| `test_recurrence.py` | 10 | Schedule math (month ends, intervals) |
| `test_recurring.py` | 16 | Rules API, generation (once per period, concurrent workers, catch-up), mark-paid/skip, reminders |
| `test_savings.py` | 8 | Goals, contributions, balance never negative, milestones |
| `test_notifications.py` | 9 | Notification list/read, preferences, device tokens |
| `test_push.py` | 11 | Expo sender against a mock transport (batching, ticket errors, token disabling, session checks) |
| `test_reports.py` | 36 | Every report, ranges/time zones, currency separation, CSV export |
| `test_dashboard.py` | 4 | Dashboard cards and scoping |
| `test_integration_fixes.py` | 11 | Category children, budget limits vs deleted categories (merge/repoint/remove), archived categories / inactive accounts, flow-owned transaction fields, `can_edit` / `can_delete` / `user_name`, configurable limits |
| `test_security_hardening.py` | 11 | Idempotent replay scope checks, contribution scope, log redaction of push tokens and emails, CSV formula injection, export/analytics/device rate limits, recurring generation authority and currency, budget currency |
| **`test_critical_scenarios.py`** | 8 | **Spec §17 scenarios 1–8 end-to-end through the HTTP API** (one test per scenario, named `test_scenario_<n>_…`) |

## Mobile

From `mobile/`:

```bash
npm ci              # or npm install
npm test            # jest
npm run typecheck   # tsc --noEmit
npm run lint        # expo lint
```

CI also runs `npx expo export --platform android --output-dir dist` (the JS bundle must build)
and `npm audit --omit=dev --audit-level=high`.

`jest.setup.js` replaces `expo-secure-store` with an in-memory map, so tests never touch native
modules.

**`__tests__/apiClient.test.ts` (8)**: tokens stored only in SecureStore. Bearer token attached.
Refresh once on 401 and retry. **Concurrent 401s share a single refresh** (no refresh-token
reuse). Session cleared and the handler notified when refresh fails. Error envelope and field
errors parsed. Network failures mapped to a friendly `ApiError`. Unauthenticated calls send no
token and never refresh.

**`__tests__/validation.test.ts` (6)**: email trimmed and lowercased. Invalid email and empty
password rejected. Password length and simplicity rules. Password must not contain the email's
local part. 6-digit code format. Change-password confirmation must match.

**`__tests__/login-screen.test.tsx` (4)**: empty form shows errors without calling the API.
Successful sign-in stores the session. Server error shown for bad credentials. Unverified users
are routed to the verification screen.

Not covered by automated tests yet: biometric `AppLock` behaviour, other screens, and on-device
end-to-end flows. Check these manually on an emulator or device before releases.

## Critical scenarios (spec §17)

All eight are proven end-to-end through the HTTP API in `backend/tests/test_critical_scenarios.py`
(setup and assertions via `/api/v1` only; scenario 8 runs the worker's own
`run_recurring` because generation has no HTTP trigger):

| # | Scenario | Test |
|---|---|---|
| 1 | User A can't read/list/update/delete user B's personal transactions or accounts — by id, list, `member_user_id` filter, summary, both CSV exports, reports, dashboard, tags, attachments | `test_scenario_1_user_cannot_access_another_users_personal_transactions` |
| 2 | Family A members can't reach family B's transactions, accounts, budgets, goals, categories or reports (every list endpoint with `family_id`, every id endpoint, cross-family references) | `test_scenario_2_family_cannot_access_another_familys_data` |
| 3 | Members and viewers can't invite, remove members, change roles, update/delete the family, transfer ownership, or create family budgets/accounts/categories (admins can't do owner-only actions); nothing changes | `test_scenario_3_member_and_viewer_cannot_perform_admin_actions` |
| 4 | UPI launch and (claimed-success) return never complete the transaction or count it anywhere; only the user's confirmation does, as `user_confirmed` | `test_scenario_4_upi_launch_and_return_never_complete_a_transaction` |
| 5 | Repeated / concurrent initiate, return and confirm calls (and generic idempotent creates) never duplicate rows or double count | `test_scenario_5_duplicate_initiate_and_confirm_never_duplicate` |
| 6 | Transfers never inflate income/expense in the summary, reports, dashboard, budgets (personal and family) | `test_scenario_6_transfers_never_inflate_income_or_expense` |
| 7 | Budget `spent` stays right through create, amount edit, category change, pending/completed, refund, delete, restore (+ history, list, report agree) | `test_scenario_7_budget_spent_stays_correct_through_edits` |
| 8 | Recurring generation run repeatedly and concurrently never duplicates a period (also after schedule edits and deleting an occurrence) | `test_scenario_8_recurring_generation_never_duplicates` |

Other security-critical behaviour (auth, sessions, uploads, logging, families, budgets alerts,
push) is covered by the per-area suites listed above.
