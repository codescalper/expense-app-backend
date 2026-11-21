# Run Alembic against a remote database (e.g. Neon for the Vercel deployment) from this machine.
#
#   backend\scripts\migrate_remote.ps1                  # alembic current, then upgrade head
#   backend\scripts\migrate_remote.ps1 current          # any other alembic arguments
#   backend\scripts\migrate_remote.ps1 downgrade -1
#
# Reads DATABASE_MIGRATION_URL (Neon *direct* host, owner role) from $env:ENV_FILE
# (default: <repo>\.env.vercel) unless it is already set in the environment. Nothing secret is
# stored in this script and the URL never appears on a command line: it is placed in the
# process environment and passed into the container by name (-e VAR).
# Runs inside the compose `api` dev image (no local Python needed): docker compose must work.
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$envFile = if ($env:ENV_FILE) { $env:ENV_FILE } else { Join-Path $repoRoot '.env.vercel' }

if (-not $env:DATABASE_MIGRATION_URL) {
    if (-not (Test-Path $envFile)) {
        throw "$envFile not found (copy .env.vercel.example) and DATABASE_MIGRATION_URL is unset"
    }
    $line = Get-Content $envFile | Where-Object { $_ -match '^\s*DATABASE_MIGRATION_URL=' } | Select-Object -Last 1
    if ($line) {
        $value = ($line -split '=', 2)[1].Trim().Trim('"').Trim("'")
        $env:DATABASE_MIGRATION_URL = $value
    }
}
if (-not $env:DATABASE_MIGRATION_URL) {
    throw "DATABASE_MIGRATION_URL is empty in $envFile"
}
if ($env:DATABASE_MIGRATION_URL -match '-pooler\.') {
    Write-Warning 'DATABASE_MIGRATION_URL points at a -pooler host; use the direct host for migrations'
}

# Show where we are going without printing the password.
$target = ($env:DATABASE_MIGRATION_URL -split '@', 2)[-1]
Write-Host ("Target: " + ($target -split '\?', 2)[0])

function Invoke-RemoteAlembic {
    param([string[]]$AlembicArgs)
    Push-Location $repoRoot
    try {
        # ENVIRONMENT/JWT_SECRET_KEY: settings are validated even for Alembic, which never signs
        # anything; a fixed non-secret placeholder keeps this independent of the local .env.
        docker compose run --rm --no-deps -T -e DATABASE_MIGRATION_URL `
            -e ENVIRONMENT=development `
            -e JWT_SECRET_KEY=alembic-only-placeholder-never-used-for-signing `
            api alembic @AlembicArgs
        if ($LASTEXITCODE -ne 0) { throw "alembic $($AlembicArgs -join ' ') failed ($LASTEXITCODE)" }
    }
    finally {
        Pop-Location
    }
}

if ($args.Count -eq 0) {
    Write-Host '== current revision'
    Invoke-RemoteAlembic @('current')
    Write-Host '== upgrading to head'
    Invoke-RemoteAlembic @('upgrade', 'head')
    Write-Host '== now at'
    Invoke-RemoteAlembic @('current')
}
else {
    Invoke-RemoteAlembic ([string[]]$args)
}
