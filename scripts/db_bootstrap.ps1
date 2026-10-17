# Bootstrap the Stoxsy database: base schema + all migrations (idempotent).
# Usage (from repo root):  .\scripts\db_bootstrap.ps1
# Native tools (docker compose) write progress to stderr; PS 5.1 would turn that into
# a terminating error under 'Stop', so we check $LASTEXITCODE explicitly instead.
$ErrorActionPreference = 'Continue'
$repo = Split-Path -Parent $PSScriptRoot
$py = Join-Path (Split-Path -Parent $repo) '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = Join-Path $repo '.venv\Scripts\python.exe' }

docker compose -f (Join-Path $repo 'docker-compose.yml') up -d 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed' }
for ($i = 0; $i -lt 30; $i++) {
    docker exec qsde_timescaledb pg_isready -U qsde 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { break }
    Start-Sleep -Seconds 2
}

# Pipe through cmd so the UTF-8 bytes of init.sql reach psql unchanged.
cmd /c "docker exec -i qsde_timescaledb psql -U qsde -d qsde -v ON_ERROR_STOP=1 < `"$repo\infra\init.sql`""
if ($LASTEXITCODE -ne 0) { throw 'init.sql failed' }

Push-Location (Join-Path $repo 'backend')
try { & $py scripts/apply_migrations.py } finally { Pop-Location }
