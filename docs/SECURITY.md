# Security

This document covers what is implemented in Phases 1–2 (foundation and authentication), the
policies that bind later phases, and known limitations. Implementation detail is in
[ARCHITECTURE.md §4](ARCHITECTURE.md#4-authentication-design).

## Threat model (summary)

| Asset | Threats considered |
|---|---|
| Account credentials | Credential stuffing, online brute force, offline cracking after a DB leak, user enumeration |
| Sessions and tokens | Token theft from device or network, refresh-token replay, use after logout or password change |
| Email codes (verify/reset) | Guessing, replay, interception of stale codes, offline brute force from a DB leak |
| Personal data (email, name, avatar, IPs in audit and sessions) | IDOR (reading or changing another user's data), over-sharing in API responses or logs |
| Financial data | Cross-user / cross-family access (IDOR), tampering by lower-privileged members, double counting (duplicate UPI callbacks, recurring re-generation), payment-credential exposure, CSV formula injection in exports |
| Server | Oversized or malicious uploads, request floods, misconfiguration (debug docs, wildcard CORS, weak secrets) |

Actors: anonymous internet attackers, someone holding a stolen or unlocked phone, a network
attacker (mitigated by mandatory HTTPS in deployment), and an attacker who has obtained a
database dump or backup, and a malicious or curious member of a shared family (contained by
family roles: viewers read only, members change only their own family records, admin-only
setup actions, owner-only ownership transfer / family deletion).

## Controls implemented

### Authentication and sessions (`backend/app/core/security.py`, `backend/app/services/auth.py`, `backend/app/api/deps.py`)

- **Argon2id** password hashing (t=3, 64 MiB, p=4 by default) with transparent rehash on
  login. Verification failures of any kind return `False` without raising.
- **Password policy:** 10–128 characters, common-password blocklist, at least 4 distinct
  characters, must not contain the email's local part. No forced composition or rotation.
  Passwords are never stripped of whitespace.
- **Short-lived access JWTs** (15 min, HS256 pinned, issuer checked, `typ=access` required).
  Every authenticated request also checks the session row (`sid`), so revocation is immediate.
- **Opaque refresh tokens**, stored as SHA-256 hashes and rotated on every use. **Reuse of a
  rotated token revokes the whole session** and is audited (`auth.refresh_token_reuse_detected`).
  The token row is locked during refresh, so races serialize.
- **Email codes:** 6 digits from `secrets`, stored as HMAC-SHA256 keyed with the server secret
  **and bound to the account's password hash at issue time** (any password change kills every
  outstanding code), compared in constant time, 30-minute expiry, burned after 5 wrong attempts,
  single use, invalidated when a newer code is issued, and **capped at 15 wrong codes per
  account and purpose per 24 h** across re-issued codes (after that verification/reset is
  refused with the generic `invalid_code` until the window passes).
- **Pre-registration takeover:** an unverified account belongs to nobody yet. Re-registering it
  replaces its password and name and invalidates earlier codes, so whoever proves control of the
  inbox ends up with the password they chose. `verify-email` also accepts the registration
  `password`; when sent, the code only verifies a registration made with that password (closes
  the race where someone re-registers *after* the real owner). Tests:
  `test_preregistration_takeover_prevented`,
  `test_verify_with_password_rejects_a_later_hostile_registration`.
- **Anti-enumeration:** register, resend-verification and forgot-password always return 202 with
  identical bodies, and do the same DB work for known and unknown addresses (quota counter bump
  plus one queued job — a `noop_email` job for unknown/ineligible addresses). Unknown emails on
  login burn an Argon2 verification to equalise timing. Login errors are generic
  (`invalid_credentials`). The "email not verified" error is only shown after a correct password.
  Registering an already-verified email sends an "account exists" email to the owner instead of
  revealing anything. Emails to unverified addresses never contain the registrant-supplied name.
- **Brute-force and abuse limits** (`backend/app/core/rate_limit.py`, stored in PostgreSQL):
  - per-IP limits on each unauthenticated auth endpoint (30 per 5 min); IPv6 clients are
    limited per **/64**
  - per-email login failures: a **decaying** counter (sliding 15-min window) and, at 5, a
    15-minute **lock** — both keyed on an HMAC of the email, never on the user row, so real and
    unknown addresses behave identically (test:
    `test_lockout_identical_for_real_and_unknown_email_across_window_boundary`)
  - per-address email quota (3 per 15 min, silently enforced)
  - re-authentication (change password, delete account): 10 requests per 15 min per user, and
    5 wrong passwords lock both for 15 min; a wrong password is **400 `invalid_password`** (not
    401, which clients treat as "refresh the session")
  - avatar uploads: 10 per hour per user

  Rate-limit keys for emails are HMACs keyed with a key derived from the server secret. Limiter
  writes use the request's own DB session/connection (one pooled connection per request); failure
  paths commit before raising so failures count even though the request fails.
- **Argon2 off the event loop:** hashing/verification runs in worker threads behind a per-process
  limit (`PASSWORD_HASH_CONCURRENCY`, default 2).
- **Session management:** device list, revoke one device, logout, logout-all. Password change
  revokes all other sessions. Password reset and account deletion revoke all sessions.
  **Absolute session lifetime** (`SESSION_ABSOLUTE_LIFETIME_DAYS`, default 60) from sign-in,
  enforced on refresh and on every authenticated request, however often the session refreshes.
- **IDOR guard:** session lookups are scoped by `user_id` in the query. Another user's session ID
  behaves exactly like a missing one (404). Test: `test_cannot_see_or_revoke_another_users_session`.
- **Account deletion** requires the current password. It soft-deletes and anonymises the account
  (email rewritten to `deleted-<id>@deleted.invalid`, unusable password hash `!`, avatar file
  removed, device name / user agent / IP cleared on all of the user's sessions). The owner of a
  family that still has other members must transfer ownership first (409
  `family_ownership_transfer_required`); other memberships are ended.
- **Names** (`full_name`) have control characters, line/paragraph separators and bidi overrides
  replaced and whitespace collapsed (joiners used by Indic scripts are kept).

### HTTP layer (`backend/app/core/middleware.py`, `backend/app/main.py`, `backend/app/core/errors.py`)

- Security headers on all responses: `nosniff`, `X-Frame-Options: DENY`,
  `Referrer-Policy: no-referrer`, a restrictive `Permissions-Policy`, COOP `same-origin`,
  `CSP default-src 'none'; frame-ancestors 'none'` (not on the docs pages), and
  `Cache-Control: no-store` on `/api/*`. **HSTS** (`max-age=63072000; includeSubDomains`) when
  `ENVIRONMENT` is staging or production.
- Request IDs are validated against `^[A-Za-z0-9-]{8,64}$` before being reflected.
- **Request body cap:** 10 MiB in general and **64 KiB on `/api/v1/auth/*`**. `Content-Length`
  over the cap is refused before the app runs, and every body is also counted as it streams
  through the ASGI `receive`, so chunked bodies without `Content-Length` get a 413 too (FastAPI
  reads bodies before auth and rate-limit dependencies run, so this is what bounds
  unauthenticated work). Avatars are also read with a hard cap of `MAX_AVATAR_BYTES + 1`.
  Tests: `test_chunked_body_over_auth_cap_rejected`, `test_chunked_body_over_general_cap_rejected`.
- CORS: explicit allowlist, no credentials, and **wildcard rejected** in staging and production.
- OpenAPI docs are forced off in production.
- **Uniform error envelope.** Validation errors return field, message and type, and **never echo
  the submitted input**, so passwords can't leak through 422s. Unhandled exceptions return a
  generic 500 and log only the exception type and stack.
- Request schemas reject unknown fields (`extra="forbid"`). Emails are normalised to lowercase,
  and the DB enforces it with `CHECK (email = lower(email))`.

### Tenant isolation and authorisation (`backend/app/core/permissions.py`)

- Every endpoint except `/health*` and the public auth flows requires a bearer token whose
  session is live (`CurrentPrincipal`).
- Every query on personal/family data is filtered in SQL by `ScopeFilter` (personal rows → their
  owner; family rows → active members of a live family, via a correlated subquery — never a
  client-supplied list). Rows loaded by id for writing go through `require_record_write`.
  Anything outside the caller's scope is **404** (existence is not leaked); a visible family the
  caller's role can't change is **403** `insufficient_family_role`.
- Cross-references are scope-checked: a transaction / recurring rule / UPI payment may only use
  accounts, payees and categories of its own scope; budgets only categories of their scope; a
  savings contribution may only link a transaction of the goal's own scope and currency
  (422 `transaction_scope_mismatch`); report filters (`member_user_id`, `account_id`,
  `parent_id`) are validated against the scope.
- Idempotent replays re-check access: a repeated `Idempotency-Key` returns the original row only
  for the same scope (else 409 `idempotency_key_reused`) and only while the caller can still see
  it (a family row after leaving the family → 404).
- Recurring rules only generate while their creator is still an active, contributing member of
  the family (left / demoted to viewer → the rule stops).
- The spec §17 scenarios (IDOR, cross-family, admin-only actions, UPI integrity, duplicates,
  transfers, budget correctness, recurring duplicates) are proven end-to-end through the API in
  `backend/tests/test_critical_scenarios.py`.

### UPI integrity (`backend/app/services/upi.py`, `backend/app/services/upi_uri.py`)

- **Opening the UPI app is never success.** `launched` and `return` never touch the transaction:
  it stays `pending` / `unverified` and out of every total (budgets, reports, dashboard,
  summary) until the user confirms.
- **Return data is untrusted.** It is reduced by `sanitize_return` (scalar values only,
  credential-like keys dropped, ≤ 30 fields, lengths capped; the raw string is never stored) and
  surfaced only as `untrusted_hint`.
- **User-confirmed ≠ verified.** "It succeeded" sets `completed` + `user_confirmed`; nothing in
  the app flow can set `verified` (reserved for a future bank / PSP integration). The UI labels
  say so.
- **No duplicates.** Initiation is keyed by `(user, Idempotency-Key)` with a transaction-scoped
  advisory lock, unique constraints on attempts / transactions / `tr_ref`, and the replay
  returns the original attempt. Confirm / cancel are row-locked and idempotent.
- **No credentials.** UPI request bodies with PIN / MPIN / password / CVV / OTP-like field names
  are rejected (422); the QR parser ignores such parameters.
- Every change is audited with before/after (`upi.confirmed`, `upi.reconciled`, …); pending
  attempts get reminders, never automatic completion. Initiate/parse are rate-limited per user.

### Uploads (`backend/app/services/storage.py`, `backend/app/services/users.py`, `backend/app/services/attachments.py`)

- Avatars are validated **by content** with Pillow. Filename and client MIME type are ignored.
  Only JPEG, PNG and WebP are accepted, and `Image.verify()` must pass.
- **Decompression-bomb guard:** avatars over 4096×4096 pixels (~16.7 MP) are rejected from the
  image header, before any decoding (400 `image_too_large`); Pillow's global
  `MAX_IMAGE_PIXELS = 25,000,000` stays as a backstop. Decoding runs in a worker thread (at most
  2 at once per process), and uploads are rate-limited per user.
- **Re-encoded** to a 512×512 JPEG (center crop), which strips EXIF/GPS metadata and any appended
  payload.
- Storage keys are server-generated (`avatars/<random>.jpg`), validated against a strict regex,
  and resolved paths must stay under `UPLOAD_DIR` (no path traversal). Files are served only
  through the authenticated `GET /users/me/avatar`, with `Cache-Control: private`.
- **Receipts** (`process_receipt`): read with a hard cap of `MAX_RECEIPT_BYTES` + 1 byte (413
  above it), type sniffed from magic bytes only. JPEG / PNG / WebP are size-checked from the
  header (≤ 4096² px) before decoding, EXIF-rotated and **re-encoded to JPEG** (long side ≤ 2000
  px, metadata and trailing payloads dropped). PDFs are accepted only with a `%PDF-` header and
  **no JavaScript, launch actions, embedded files or encryption**. Max 10 live attachments per
  transaction (re-checked under a row lock), uploads rate-limited per user
  (`RECEIPT_UPLOAD_LIMIT`, rejected files count), decoding in a bounded thread pool. Upload /
  delete need write access to the transaction; download needs visibility. Downloads are served
  as `Content-Disposition: attachment` with the sniffed content type.

### Logging and audit (`backend/app/core/logging.py`, `backend/app/repositories/system.py`)

- Structured logs with **recursive redaction**. Keys containing `password`, `passwd`, `secret`,
  `token`, `authorization`, `cookie`, `cvv` or `api_key`, and exact keys `code`, `otp`, `pin`,
  `upi_pin`, `mpin` and `card_number`, are replaced with `[REDACTED]`. The same redaction is
  applied to `audit_logs.details`.
- No request bodies or headers are logged. The access log records method, path, status, duration
  and request ID. Push tokens in paths (`DELETE /notifications/devices/{token}`) are logged as
  `<redacted>`; the push sender logs only masked tokens (`mask_token`) and never message bodies.
  Worker error text has email addresses replaced by `<email>` before it is logged or stored in
  `background_jobs.last_error`.
- Tracebacks are plain (no local variables — the dev console renderer's rich tracebacks would
  print passwords held in locals), and the SQLAlchemy engine uses `hide_parameters=True`, so bound
  values never appear in DB error messages.
- Audit trail for security events (register, verify, login, failed login, lockout, refresh reuse,
  logout, logout-all, password change and reset, session revoke, profile and avatar changes,
  account deletion) and for every financial / family change (before/after values on updates),
  with IP, user agent and `family_id`.

### Push tokens (`backend/app/services/notifications.py`, `backend/app/services/push.py`)

- Expo push tokens are bound to the session that registered them. Logout / session revoke /
  password change disables the affected tokens immediately; the sender also disables tokens
  whose session is revoked, expired or past `SESSION_ABSOLUTE_LIFETIME_DAYS`, and tokens Expo
  reports as `DeviceNotRegistered`. Users with no usable token get `push_status=skipped`.
- A token is unique: registering it again (e.g. another account signs in on the same phone)
  re-binds it to the new session. Registration is rate-limited per user
  (`DEVICE_REGISTER_RATE_LIMIT`), tokens are never returned in full by the API (`token_hint`)
  and never logged.
- Push payloads carry the same content as the in-app notification (title, body, `data` with
  ids and a deep link) — never credentials or tokens.

### CSV exports (`backend/app/utils/csv_safety.py`)

- Both exports (`/transactions/export.csv`, `/reports/export.csv`) pass every text cell through
  `neutralize_formula`: a leading `= + - @`, TAB / CR / LF, their full-width forms, or any of
  these behind leading whitespace gets a `'` prefix. Numbers and dates we generate are written
  as-is. Exports are scoped like the list endpoints and rate-limited per user
  (`EXPORT_RATE_LIMIT`).

### Data at rest

- No plaintext secrets in the DB: passwords (Argon2id), refresh tokens (SHA-256), email codes
  (HMAC).
- Email job payloads contain the one-time code in plaintext until sent. **Payloads are wiped** as
  soon as the job is done or permanently failed. Payloads carry an `expires_at`: the worker drops
  and wipes a job that is past it instead of sending, and fails (and wipes) a job immediately when
  its next retry would only run after expiry.
- Rate-limit keys never contain raw emails (HMAC with a server-secret-derived key).

### Configuration hardening (`backend/app/core/config.py`)

- `DATABASE_URL` and `JWT_SECRET_KEY` have no defaults. The secret must be at least 32 characters.
- In staging and production, placeholder-looking secrets (`change-me`, `dev-only`, …), `*`
  CORS and `FORWARDED_ALLOW_IPS=*` stop the app from starting. The prod image sets
  `FORWARDED_ALLOW_IPS=127.0.0.1` by default and its CMD does not pass `--forwarded-allow-ips`,
  so uvicorn uses the value from the environment; set it to your proxy's addresses at deploy time.
- Containers run as a non-root user (`app`, uid 999). The `prod` image contains only `app/` and
  `alembic/`, with no tests or dev tooling.
- Compose binds Postgres and Mailpit to `127.0.0.1` only.

### Mobile (`mobile/src/`)

- Tokens are stored **only** in `expo-secure-store` (Keychain/Keystore,
  `WHEN_UNLOCKED_THIS_DEVICE_ONLY`). Never AsyncStorage, never logs. On web (dev only) they are
  kept in memory. Test: `stores tokens only in SecureStore`.
- **Single-flight refresh** prevents the app from accidentally triggering reuse detection.
- The session is cleared locally on refresh failure. Sign-out clears tokens and the whole query
  cache even if the server is unreachable.
- Optional **biometric app lock** that re-locks after 60 s in the background. Changing the
  setting itself requires authentication.
- `EXPO_PUBLIC_*` is the only build-time configuration and contains no secrets.

### Supply chain (CI, `.github/workflows/ci.yml`)

- Pinned Python dependencies (`requirements*.txt` compiled with uv). `pip-audit` runs on every
  push and PR.
- `npm ci` with lockfile, and `npm audit --omit=dev --audit-level=high`.
- Ruff with security rules (`S`, bandit-derived) and strict mypy.

## Secrets

| Secret | Purpose | Notes |
|---|---|---|
| `JWT_SECRET_KEY` | Signs access JWTs **and** keys the email-code HMAC | At least 32 chars (48+ random bytes recommended). Unique per environment. **Rotation:** set the new value and restart the API. All current access tokens become invalid (clients refresh transparently, because refresh tokens don't depend on this key) and all outstanding email codes stop working (users request a new one). There is no multi-key (`kid`) support, so rotation is a hard cut-over |
| DB passwords (`DATABASE_URL`, `DATABASE_MIGRATION_URL`) | App role and owner role | Different roles for runtime and migrations ([NEON.md](NEON.md#least-privilege-roles)). Rotate in Neon, then update the env and restart |
| `SMTP_PASSWORD` | Outgoing mail | Only the worker needs it |

Rules:

- Never commit `.env`. `.gitignore` excludes `.env`, `.env.*` (except `.env.example`), `*.pem`
  and `*.key`.
- Inject secrets through your platform's secret store or environment, not baked into images.
  `backend/.dockerignore` excludes `.env*`.
- Never put secrets in `mobile/.env`. `EXPO_PUBLIC_*` values ship inside the app bundle.

## What is never stored

This policy is binding for all later phases (UPI, accounts, cards):

- **UPI PINs / MPINs.** UPI payments will use **intent handoff** to the user's own UPI app. The
  PIN is entered only in that app and never passes through this app or server.
- **Card numbers (PAN), CVV/CVC, card expiry or PINs.** At most a user-chosen nickname and
  optionally the last 4 digits.
- **Net-banking or bank-app usernames and passwords, bank OTPs.**
- **Raw SMS or bank-statement credentials** (no screen-scraping integrations).

Defence in depth: log and audit redaction already masks `cvv`, `pin`, `upi_pin`, `mpin`,
`card_number`, `otp` and anything containing `password`/`secret`/`token`. Code review must
reject any schema or model that adds such a column or request field.

Stored for UPI: payee VPA and name, amount, note, our transaction reference (`tr_ref`), the
idempotency key, the user-reported outcome and optional UTR, and a sanitised copy of the UPI
app's return data. Outcomes are self-reported and stored as `user_confirmed` at most, never
`verified`. See [ER.md](ER.md#2-families-finance-planning-upi-notifications-revisions-00020003).

## Known limitations

- **No MFA/TOTP yet.** Email + password only.
- **Targeted lockout:** anyone who knows a user's email can lock sign-in for that address for 15
  minutes by failing 5 logins. This is a deliberate trade-off against online guessing. The lock
  lifts automatically and a password reset clears it. Likewise, 15 wrong verification/reset codes
  block that flow for the account for up to 24 h (needed to stop slow code guessing across
  re-issued codes), and an attacker holding a stolen access token can lock the re-auth
  endpoints for 15 minutes.
- **Lock state lives only in `rate_limit_counters`.** The old per-row lockout
  (`users.failed_login_count` / `locked_until`) is no longer consulted because it behaved
  differently for real and unknown addresses. Truncating that table clears all locks.
  `RATE_LIMIT_ENABLED=false` disables every limit and lock (dev only).
- **Pre-registration race needs the client's help.** Re-registration replaces an unverified
  account's password, so if someone re-registers the address *after* the real owner but before
  they verify, the newest code belongs to the other registration. Clients that send `password`
  to `verify-email` are immune (the code is refused and the user registers again); clients that
  don't would verify whichever registration came last. The mobile app should send it.
- **Timing equalisation is approximate.** Unknown-address paths mirror the quota bump, job insert
  and an UPDATE round trip, but the known path still inserts one `user_tokens` row more (sub-ms).
  `verify-email` returns faster for unknown addresses unless `password` is sent.
- **Concurrency limits are per process.** The Argon2 and image-decoding thread limits apply per
  uvicorn worker process, not globally.
- **Lost refresh responses:** if the server rotates a refresh token but the response never
  reaches the phone (network drop), the app's next refresh presents the old token. That is
  treated as reuse, the session is revoked, and the user must sign in again. Safe, but
  occasionally annoying.
- **HS256 with a single shared key.** No key IDs, so rotation logs out all access tokens at once
  (see [Secrets](#secrets)).
- **Body cap is per request, not per connection.** The app now caps streamed bodies too, but slow
  uploads (slowloris) and connection counts are the reverse proxy's job; keep a body limit and
  timeouts there as well ([DEPLOYMENT.md](DEPLOYMENT.md)).
- **IP-based limits depend on correct proxy config.** Without `--proxy-headers` and a correct
  `FORWARDED_ALLOW_IPS`, all clients share the proxy's IP and hit the per-IP limit together.
  Users behind one NAT or carrier-grade NAT share a budget even when configured correctly.
- **500 responses may lack the custom headers.** Starlette sends unhandled-exception responses
  from its outermost error middleware, outside `RequestContextMiddleware`, so those responses
  likely don't carry `X-Request-ID` or the security headers. The error is still logged with the
  request ID.
- **At-least-once jobs:** a worker crash between sending and marking `done` can resend an email.
  Failed jobs have their payload wiped, so a failed email can't be inspected or replayed (the
  user requests a new code).
- **Local-disk uploads** aren't encrypted by the app. Use encrypted volumes. Multiple API hosts
  need shared storage until S3 support lands (planned).
- **Audit logs** have no retention policy, no tamper-evidence, and no admin UI. They keep IPs and
  user agents of deleted users' past actions.
- **Mobile:** the app lock is a UI overlay, not encryption. There is no certificate pinning and
  no root/jailbreak detection. Screenshots and app-switcher previews aren't blocked.
- **No email-change flow** yet, and no "new sign-in" notification emails.
- **Push-token re-binding:** a token presented by another signed-in user is moved to that
  user's session (needed when a phone changes hands or the app is reinstalled). Someone who
  obtains a victim's raw Expo token could redirect that device's pushes; tokens are never
  logged or returned in full, and registration is rate-limited, which keeps this theoretical.
- **No per-user caps on the number of records** (transactions, categories, budgets, rules…).
  Request rates are limited, record counts are not.
- **Transaction summary across currencies:** `/transactions/summary` adds amounts of every
  currency in the filtered set (reports and budgets don't: reports take a `currency` and
  budgets count only their own currency). Single-currency households (the INR default) are
  unaffected.
- **Duplicate links:** the same transaction may back several savings contributions (scope and
  currency are enforced, uniqueness isn't).
- **Recurring rules of a former member** stop generating; an admin has to recreate them (there
  is no "take over rule" action yet).

## Reporting a vulnerability

Please **don't open a public issue** for security problems.

- If the repository is hosted on GitHub, use **Security → Report a vulnerability** (private
  vulnerability reporting) on the repository page.
- Otherwise contact the maintainer privately.

*Maintainer: add a dedicated security contact here before the first public deployment.*

Include affected version or commit, steps to reproduce, impact, and any proof-of-concept. Please
allow reasonable time for a fix before disclosure. Never test against other people's accounts or
data. Use a local stack (`docker compose up -d --build`).
