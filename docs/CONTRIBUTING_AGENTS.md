# Conventions for parallel feature work

Several contributors work in this repo **at the same time**. Follow these rules exactly so nobody
breaks anyone else.

## Ground rules
- **Only edit files you own** (listed in your task). Need a change in someone else's file? Don't make
  it — list it under "Requested changes" in your final report.
- **Never** `git commit`, `git stash`, `git checkout` or reset anything. Never delete files you didn't create.
- **No Redis.** PostgreSQL only. Background work = `background_jobs` table via
  `app/workers/jobs/<module>.py` (`HANDLERS` / `PERIODIC`, auto-discovered — see the package docstring).
- Read `docs/PRODUCT_SPEC.md` for requirements and the existing code before writing.
- No placeholder business logic or fake data in features you mark done. Don't claim something works
  unless a test or command showed it.

## Backend (`backend/`)
- Layering: `api/v1/endpoints/<module>.py` (thin; module-level `router` is auto-registered) →
  `services/<module>.py` (business rules, owns `commit()`) → `repositories/<module>.py` (queries) →
  `models/` . Schemas in `schemas/<module>.py` (`RequestModel` forbids unknown fields).
- Auth: `CurrentPrincipal` from `app/api/deps.py`. Family authorisation via `app/core/permissions.py`.
- **Tenant isolation:** every query on family/personal data filters by what the principal may see.
  Resources outside the caller's scope return **404** (not 403) so existence isn't leaked; a member
  lacking a *role* for a visible family gets 403.
- Money: `Numeric(14,2)` / `Decimal`; serialise amounts as strings; never float. Currency default `INR`.
- Multi-step financial writes in one DB transaction. Record financial changes with `AuditRepository`.
- Soft delete (`deleted_at`) for financial records; queries exclude deleted rows.
- Errors: raise `AppError` subclasses from `app/core/errors.py`.
- **Migrations:** only the contributor whose task says so creates Alembic revisions.

### Running backend checks without colliding
Each contributor uses **their own test database** (name given in your task, e.g. `family_budget_test_tx`):

```bash
cd expense   # repo root (Git Bash)
docker compose exec -T postgres psql -U postgres -c "CREATE DATABASE family_budget_test_tx" || true
docker compose run --rm --no-deps \
  -e TEST_DATABASE_URL=postgresql+asyncpg://postgres:postgres@postgres:5432/family_budget_test_tx \
  api pytest -q tests/test_transactions.py
docker compose run --rm --no-deps api ruff check app/services/transactions.py ...   # your files only
docker compose run --rm --no-deps api ruff format app/services/transactions.py ...  # your files only
docker compose run --rm --no-deps api mypy app      # ignore errors in files you don't own
```
If a failure comes from a file someone else is mid-way through editing (e.g. a half-written
migration), wait a minute and retry rather than "fixing" their file.

Test helpers: `tests/conftest.py` (`register_verified`, `bearer`, `login`, `latest_code`). Put
module-specific helpers in your own test file or `tests/helpers_<module>.py`.

## Mobile (`mobile/`)
- Expo SDK 58. Routes live in `src/app/` (Expo Router). Your feature's screens go in your own folder
  (e.g. `src/app/transaction/`). Signed-in route folders are declared in `src/app/_layout.tsx`.
- API: create `src/api/<module>.ts` with typed functions **and** that module's query keys. Use
  `apiRequest` from `src/api/client.ts`. Never hardcode the server URL (it's user-configurable).
- UI: use `src/components/ui` and `src/components/charts` + `src/theme` tokens only (no raw hex).
  Money formatting/parsing from `src/utils/money.ts`; dates from `src/utils/dates.ts`.
- Forms: React Hook Form + Zod (`src/validation/<module>.ts`). Wrap submit as `() => void onSubmit()`.
- Active scope (personal vs a family): `src/store/scope.ts`.
- Tests: `__tests__/<module>*.test.ts(x)` (jest-expo + RNTL; mock `fetch`). Run
  `npx tsc --noEmit`, `npx expo lint`, `npx jest __tests__/<yours>` from `mobile/` — ignore errors in
  files you don't own. Don't run `npm install`/`expo install` (deps are pre-installed; request others).

## Final report (every contributor)
Files created/changed · what works (with the exact commands you ran and results) · what's not done ·
"Requested changes" to files you don't own · API contract summary (endpoints + payload shapes) for
the mobile/backend counterpart.
