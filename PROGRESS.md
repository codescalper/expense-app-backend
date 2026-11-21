# Progress

Last updated: 2026-11-20

## Phase status

| Phase | Scope | Status |
|---|---|---|
| 1 Foundation | Monorepo, FastAPI, Expo, Docker Compose, Postgres, Alembic, CI, docs | ✅ Done, verified |
| 2 Authentication | Register/verify, login, refresh rotation, logout(all), reset, profile, avatar, sessions, delete account, biometric lock | ✅ Done; independent security review → all 15 findings fixed with regression tests |
| 3 Family system | Families, invitations, roles/permissions, personal vs family scope | ✅ Backend done; mobile UI in progress |
| 4 Finance core | Accounts, categories, payees, transactions, transfers, attachments, dashboard | ✅ Backend done (migrations 0002/0003); mobile in progress |
| 5 UPI | QR parse, pending txn, intent URI, launch/return/confirm, idempotency, reminders | ✅ Backend done; mobile in progress |
| 6 Planning | Budgets, category budgets, recurring bills, savings goals | ✅ Backend done; mobile in progress |
| 7 Notifications & analytics | Push, reminders, reports, CSV export | ✅ Backend done (Expo push sender, 11 reports, dashboard); mobile chart kit done |
| 8 Hardening & deploy | Security testing, perf, CI/CD, prod deployment | 🚧 Backend integration & hardening pass done (below); prod deployment pending |

## Verified so far (commands actually run)

- Backend: `ruff check`, `ruff format --check`, `mypy --strict` clean; **45/45 pytest** passing against real
  PostgreSQL (includes Alembic downgrade→upgrade round-trip); `pip-audit`: no known vulnerabilities.
- Full stack E2E via Docker Compose: register → verification email delivered by the worker over SMTP
  to Mailpit → verify → `/users/me` → refresh rotation → replay of an old refresh token revokes the
  session (401) → audit rows written.
- Production image (`--target prod`) builds and runs as non-root `app` user.
- Mobile: `tsc --noEmit` clean, `expo lint` clean, **30/30 jest** passing, Android JS bundle exports
  (`expo export --platform android`). `expo-doctor`: 18/20 (the 2 failures are network checks
  that couldn't reach exp.host from this machine).
- Runtime-configurable backend URL: login screen → "⚙ Server" (validate, test connection via
  `/health/ready`, save on device, reset to default; warns on public `http://`).

## Wave 1 results (parallel agents)

- Backend: 107 tests passing (auth hardening, families, permissions, ledger rules, notify helper).
- Mobile: auth hardening (refresh only signs out on real 401s, fail-closed app lock, avatar cache),
  shared UI kit + SVG charts + money/date utils (118 tests).

## Backend integration & hardening pass (2026-11-20) — verified results

Commands run from the repo root (Git Bash) against the compose stack; own test DB
`family_budget_test_integration`:

| Gate | Command | Result |
|---|---|---|
| Full suite, run 1 | `docker compose run --rm --no-deps -e TEST_DATABASE_URL=…/family_budget_test_integration api pytest -q` | **418 passed** (6 m 43 s) |
| Full suite, run 2 (consecutive, same command) | same | **418 passed** (8 m 24 s) |
| Lint / format | `api ruff check .` · `api ruff format --check .` | All checks passed · 142 files already formatted |
| Types | `api mypy app` (strict) | no issues in 113 source files |
| Migrations | on scratch DB `family_budget_scratch_integration`: `alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head && alembic check` | head `0003`; "No new upgrade operations detected." (both checks) |
| Dependencies | `MSYS_NO_PATHCONV=1 docker compose run --rm --no-deps -e HOME=/tmp api pip-audit -r requirements.txt --cache-dir /tmp/pa` | No known vulnerabilities found |
| Prod image | `docker build --target prod -t family-budget-api:ci backend` | built |
| Worker discovery | `docker compose restart worker` → `worker_started` log | `job_kinds=['noop_email','send_email']`, `periodic=['budget_alerts','unusual_spending','push_sender','recurring','upi_pending_reminders']`; no errors afterwards |
| Concurrent runs | two pytest runs on one DB | second prints "another test run is using …; waiting" and both pass |

Changes in this pass:

- **Spec §17 scenarios 1–8 proven end-to-end via HTTP**: `backend/tests/test_critical_scenarios.py`.
- Categories: `GET /categories/{id}` children verified for personal/family (regression tests) and
  built-ins now take `?family_id=` to list a family's subcategories. Category delete now moves
  budget limits with `reassign_to` (merging amounts into an existing limit) or removes them —
  same DB transaction; budgets ignore limits on deleted categories.
- Transactions / recurring rules / UPI reject **archived categories** (422 `category_archived`,
  incl. children of an archived parent) and inactive accounts for *new* references; existing
  rows stay editable. Generic PATCH can't touch `source/verification/external_ref/recurring_id/
  period_key/upi_attempt_id/idempotency_key` (tested).
- API additions: `RecurringOut.can_edit`; `ContributionOut.user_name` + `can_delete`;
  `CategoryDeleteOut.moved_budget_limits` / `removed_budget_limits`; dashboard
  `recent_transactions[].upi_attempt_id`.
- Receipt size/upload limits and UPI rate limits moved to Settings (same defaults).
- Security fixes (tests in `test_security_hardening.py`): idempotent replay re-checks scope and
  membership; savings contributions only link same-scope/same-currency transactions; push tokens
  redacted from access logs; emails stripped from worker error text; CSV formula neutralisation
  hardened (whitespace / full-width prefixes) and shared by both exports; per-user rate limits
  on CSV exports, `/reports/*` + `/dashboard`, push-token registration; family recurring rules
  stop generating when their creator leaves/is demoted; recurring currency must match its
  accounts; budget spending counts only the budget's currency; row locks on recurring
  edits/mark-paid.
- **Flaky tests** (diagnosis by elimination — none of them could be reproduced once runs were
  isolated): the three intermittent failures (`test_pending_reminders`,
  `test_create_internal_and_mark_status`, `test_revoked_or_expired_session_token_disabled`) are
  the tests that check whole-table state (UPI reminder scan, global notification list, push
  sender); they broke when two pytest runs shared one test database (each run drops the schema
  and truncates all tables around every test; a failed commit after a concurrent truncate then
  expired ORM objects → `MissingGreenlet` on attribute access). Fix: conftest holds a
  PostgreSQL advisory lock on the test DB for the whole run (a second run waits). The redundant
  per-test truncate fixture in `tests/helpers_transactions.py` was removed (conftest truncates
  before and after every test). Verified: forced-GC full run (no unraisable/leaked
  connections), 3× stress runs of the affected modules, 2× full suite — all green.
- CI: `alembic check` added after `alembic upgrade head`.
- Docs updated: ER (full implemented schema), MIGRATIONS (0002/0003), ENVIRONMENT (every
  Settings field), ARCHITECTURE, SECURITY, TESTING, docs/api/*.

## Decisions

- **No Redis** (owner decision). Rate limits → `rate_limit_counters`; background jobs →
  `background_jobs` + `python -m app.workers.worker` (`FOR UPDATE SKIP LOCKED`, retries, dedupe keys,
  payload wiping). Emails use an outbox pattern.
- Email verification and password reset use 6-digit codes (work on mobile without deep links),
  stored as HMACs with attempt limits.
- Endpoint modules and worker job modules are auto-discovered (no shared registry edits).
- Mobile routes live in `mobile/src/app/` (Expo SDK 58 convention).

## Known limitations / open items

- Accepted DoS trade-offs (documented in docs/SECURITY.md): anyone can lock sign-in for an email
  for 15 min; code flows can be blocked up to 24 h for an account.
- Backend limitations left consciously (docs/SECURITY.md "Known limitations"): push-token
  re-binding to a new account's session; no per-user caps on record counts;
  `/transactions/summary` sums all currencies in the filtered set (reports/budgets don't);
  a transaction can back several savings contributions; a departed member's family recurring
  rules stop and must be recreated by an admin; `users.failed_login_count/locked_until` are
  legacy columns (unused) awaiting a contract migration.
- `npm audit`: 20 moderate advisories in Expo transitive deps (0 high/critical; CI gate is `high`).
- Device testing (Expo Go / dev build) not yet done by a human; biometric lock needs a dev build for Face ID.
- No project license chosen yet (Expo's template LICENSE was removed from `mobile/`).

## Next steps

Waves of parallel work: schema + families → finance core, planning, notifications backend →
UPI, reports backend + mobile feature screens → integration pass. See git history / this file.

## Production deployment (2026-11-21)

- **API:** https://expense-app-backend-psi.vercel.app (Vercel, project `expense-app-backend`, account
  codescalper, Hobby plan, function region `iad1` next to Neon `us-east-2`).
- **Database:** Neon Postgres 18, migrated to revision `0004` (`backend/scripts/migrate_remote.sh`).
- **Storage:** Neon S3-compatible bucket `expense` (private; downloads stream through the API).
- **Email:** Gmail SMTP, `EMAIL_DELIVERY=inline`.
- **Cron:** Vercel daily cron (`17 3 * * *`) hits `/internal/cron/tick`. For timely reminders and
  push on Hobby, add an external pinger (cron-job.org) every 5 min with
  `Authorization: Bearer <CRON_SECRET>` (see docs/VERCEL.md).
- **Verified live (25/25 end-to-end checks):** health/ready, docs hidden, cron auth, real verification
  email via Gmail, register/login/refresh, accounts + balances, built-in categories, expenses,
  UPI initiate/idempotent replay/never-counted-before-confirm/confirm, avatar upload+download via S3,
  budgets, dashboard, reports, CSV export, cron tick, cross-user isolation (404), account deletion.

## Mobile release

- APK: `dist/FamilyBudget-1.0.0-arm64.apk` (release, R8 + resource shrinking, arm64-v8a, ~20 MB),
  default server = the production API (changeable on the login screen under Server).
- Verified: tsc, expo lint, 519 jest tests, expo-doctor 20/20, design gates (anti-slop, motion,
  coherence) clean, permissions audited (no audio/location/overlay).
- **Not yet verified on a physical device** (user chose to test it). Signed with the debug keystore:
  fine for sideloading; see mobile/docs/RELEASE.md for a Play Store upload key.
