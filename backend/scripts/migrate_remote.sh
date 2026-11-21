#!/usr/bin/env bash
# Run Alembic against a remote database (e.g. Neon for the Vercel deployment) from this machine.
#
#   backend/scripts/migrate_remote.sh                 # alembic current, then upgrade head
#   backend/scripts/migrate_remote.sh current         # any other alembic arguments
#   backend/scripts/migrate_remote.sh downgrade -1
#
# Reads DATABASE_MIGRATION_URL (Neon *direct* host, owner role) from ENV_FILE
# (default: <repo>/.env.vercel). Nothing secret is stored in this script and the URL is never
# put on a command line: it is exported and passed into the container by name (-e VAR).
# Runs inside the compose `api` dev image (no local Python needed): docker compose must work.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$REPO_ROOT/.env.vercel}"

if [[ -z "${DATABASE_MIGRATION_URL:-}" ]]; then
  if [[ ! -f "$ENV_FILE" ]]; then
    echo "error: $ENV_FILE not found (copy .env.vercel.example) and DATABASE_MIGRATION_URL is unset" >&2
    exit 1
  fi
  # Read just this one key; no `source` (values contain ? and & which a shell would mangle).
  line="$(grep -E '^[[:space:]]*DATABASE_MIGRATION_URL=' "$ENV_FILE" | tail -n 1 || true)"
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  DATABASE_MIGRATION_URL="$value"
fi
if [[ -z "${DATABASE_MIGRATION_URL:-}" ]]; then
  echo "error: DATABASE_MIGRATION_URL is empty in $ENV_FILE" >&2
  exit 1
fi
case "$DATABASE_MIGRATION_URL" in
  *-pooler.*) echo "warning: DATABASE_MIGRATION_URL points at a -pooler host; use Neon's direct host for migrations" >&2 ;;
esac
export DATABASE_MIGRATION_URL

# Show where we are going without printing the password.
target="${DATABASE_MIGRATION_URL#*@}"
echo "Target: ${target%%\?*}"

run_alembic() {
  # ENVIRONMENT/JWT_SECRET_KEY: settings are validated even for Alembic, which never signs
  # anything; a fixed non-secret placeholder keeps this independent of the local .env.
  (cd "$REPO_ROOT" && docker compose run --rm --no-deps -T \
    -e DATABASE_MIGRATION_URL \
    -e ENVIRONMENT=development \
    -e JWT_SECRET_KEY=alembic-only-placeholder-never-used-for-signing \
    api alembic "$@")
}

if [[ $# -eq 0 ]]; then
  echo "== current revision"
  run_alembic current
  echo "== upgrading to head"
  run_alembic upgrade head
  echo "== now at"
  run_alembic current
else
  run_alembic "$@"
fi
