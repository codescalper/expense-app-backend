# Backup and restore

What holds state:

| State | Where | How to back it up |
|---|---|---|
| All relational data, including rate-limit counters and the job queue | PostgreSQL (`family_budget`) | `pg_dump` locally. Neon history plus `pg_dump` in hosted environments |
| Avatars (receipts later) | `UPLOAD_DIR`: compose volume `family-budget_uploads` at `/data/uploads` | Copy the directory or volume |
| Secrets (`JWT_SECRET_KEY`, SMTP password, DB passwords) | Your secret store / `.env` | Keep them in a password manager or secret manager, separately from data backups |

Losing `JWT_SECRET_KEY` doesn't lose data. Restoring with a new key only invalidates current
access tokens (apps refresh transparently, because refresh tokens are SHA-256 hashed without a
key) and any outstanding email codes.

Dumps contain emails, password hashes, IP addresses and (later) financial data. **Encrypt them
at rest and restrict access.** Don't keep them inside the repo: `.gitignore` doesn't cover a
`backups/` folder. The examples use `../fb-backups` next to the repo.

## Local compose database

Run these from the repo root with the stack up (`docker compose up -d`).

> **Git Bash users:** run `export MSYS_NO_PATHCONV=1` first in the session. Without it Git Bash
> rewrites container paths like `/tmp/x.dump` into `C:/Users/.../Temp/x.dump`. **PowerShell
> users:** don't pipe or redirect (`>`/`<`) binary dumps. Windows PowerShell 5.1 re-encodes
> redirected output and corrupts the dump, and it has no `<` operator. The commands below write
> the dump inside the container and copy it with `docker compose cp`, which works the same in
> every shell.

### Backup

```bash
mkdir -p ../fb-backups      # PowerShell: New-Item -ItemType Directory -Force ..\fb-backups

docker compose exec -T postgres pg_dump -U postgres -d family_budget -Fc -f /tmp/family_budget.dump
docker compose cp postgres:/tmp/family_budget.dump ../fb-backups/family_budget-2026-11-20.dump
docker compose exec -T postgres rm /tmp/family_budget.dump

# sanity check: list the archive's table of contents
docker compose cp ../fb-backups/family_budget-2026-11-20.dump postgres:/tmp/check.dump
docker compose exec -T postgres pg_restore -l /tmp/check.dump
docker compose exec -T postgres rm /tmp/check.dump
```

`-Fc` (custom format) is compressed and lets `pg_restore` restore selectively. Use a date or
timestamp in the file name.

### Restore (replaces the local database)

```bash
docker compose stop api worker      # nothing may hold connections to family_budget

docker compose cp ../fb-backups/family_budget-2026-11-20.dump postgres:/tmp/restore.dump
docker compose exec -T postgres dropdb -U postgres --if-exists --force family_budget
docker compose exec -T postgres createdb -U postgres family_budget
docker compose exec -T postgres pg_restore -U postgres -d family_budget --no-owner --exit-on-error /tmp/restore.dump
docker compose exec -T postgres rm /tmp/restore.dump

docker compose start api worker     # api runs `alembic upgrade head` on start
```

If the dump comes from an older revision, the `api` start-up upgrades it to the current head.

To inspect a backup **without** touching the dev DB, restore it into a scratch database:

```bash
docker compose exec -T postgres createdb -U postgres fb_restore_check
docker compose cp ../fb-backups/family_budget-2026-11-20.dump postgres:/tmp/restore.dump
docker compose exec -T postgres pg_restore -U postgres -d fb_restore_check --no-owner --exit-on-error /tmp/restore.dump
docker compose exec -T postgres psql -U postgres -d fb_restore_check -c "SELECT version_num FROM alembic_version" -c "SELECT count(*) FROM users"
docker compose exec -T postgres dropdb -U postgres fb_restore_check
```

### Starting completely fresh

`docker compose down -v` deletes **both** volumes (`pgdata` and `uploads`). The next
`docker compose up` re-runs `infra/postgres/init.sql`, which recreates `family_budget_test`, and
the migrations. Take a backup first if you care about the data.

## Uploads volume

Back up the database and uploads together. `users.avatar_key` points at files. A missing file
makes `GET /users/me/avatar` return 404 even though `has_avatar` is true. Orphan files are harmless.

```bash
# backup (works while api is running)
docker compose cp api:/data/uploads ../fb-backups/uploads-2026-11-20

# restore: copy the contents back, then fix ownership for the non-root app user (uid 999)
docker compose cp ../fb-backups/uploads-2026-11-20/. api:/data/uploads/
docker compose exec -u root api chown -R app:app /data/uploads
```

In production, back up the volume or host directory mounted at `UPLOAD_DIR` with your platform's
volume snapshots, or `tar` it from the host. The same ownership rule applies: files must be
writable by uid 999 (`app`). The planned S3-compatible storage would replace this with bucket
versioning and replication.

## Neon

### Point-in-time restore (PITR)

Neon keeps a history of every branch for the **history retention** window configured on the
project. The maximum depends on your plan. Check *Project settings → Storage / History
retention* and set it to the longest window you can afford for production.

- **Restore a branch in place** (for example after a bad migration or accidental deletes). Go to
  Console → *Backup & Restore* (or the branch page → *Restore*), pick the timestamp just before
  the incident, and restore. Neon keeps the pre-restore state as a backup branch, so the
  operation can be undone. The branch's compute endpoint and **connection strings stay the same**.
  Connections are interrupted briefly.

  CLI equivalent (check `npx neonctl branches restore --help` for your version):

  ```bash
  npx neonctl branches restore production ^self@2026-11-20T08:00:00Z --preserve-under-name production_before_restore
  ```

- **Recover selectively.** Create a *new branch* from a past point in time (Console → *Branches →
  New branch* → choose a past timestamp/LSN). Inspect it with its own connection string, then copy
  the rows you need back into production with `pg_dump --data-only -t <table>` / `pg_restore`,
  or with SQL. Production is left alone until you're sure.

After any restore:

1. Run `alembic current` against the restored branch (direct URL) and confirm it matches what the
   deployed code expects. If it is behind, run `alembic upgrade head`.
2. Restart the worker. Jobs that were `running` at the restore point are requeued by
   housekeeping after 10 minutes. Expect possible duplicate emails, because delivery is
   at-least-once.
3. Restore uploads from the same point in time, if possible.

### Off-platform dumps

PITR protects against mistakes, not against losing the Neon account or project. Also take
periodic logical dumps (for example nightly from CI or a cron host) with the **direct**
(non-pooler) URL. The local compose postgres container has a matching `pg_dump` (v17). The
client must be at least as new as the server.

```bash
docker compose exec -T postgres pg_dump "postgresql://fb_owner:<pw>@ep-xxxx.ap-southeast-1.aws.neon.tech/family_budget?sslmode=require" -Fc -f /tmp/prod.dump
docker compose cp postgres:/tmp/prod.dump ../fb-backups/prod-2026-11-20.dump
docker compose exec -T postgres rm /tmp/prod.dump
```

Then encrypt the file and upload it to separate storage (another provider or account).

## Restore drill checklist

Do a drill at least quarterly and after any schema-heavy release. Record the date, the duration
and any problems.

- [ ] Pick a backup: the latest nightly dump **and** a Neon PITR timestamp.
- [ ] Restore into an **isolated** target: a scratch local DB (`fb_restore_check`) or a new Neon
      branch. Never restore into production for a drill.
- [ ] `pg_restore` finishes with `--exit-on-error` and no errors.
- [ ] `SELECT version_num FROM alembic_version` matches the expected head. `alembic upgrade head`
      against the restored DB is a no-op or succeeds.
- [ ] Row counts are plausible for `users`, `user_sessions`, `audit_logs` (and the financial tables
      once they exist), compared with the source at that time.
- [ ] Start the API against the restored DB (`DATABASE_URL` → restored target). `/health/ready`
      returns `{"database": "up"}`, and an existing test account can sign in.
- [ ] Uploads from the same date restore. `GET /api/v1/users/me/avatar` works for a user with an
      avatar.
- [ ] Time the whole process and compare it with your recovery-time target.
- [ ] Delete the scratch DB or branch and securely delete decrypted dump copies.
- [ ] Update this document if any step was wrong.
