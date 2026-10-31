# SUPERSEDED (archived 2026-10-31): this launcher starts the old Next.js frontend (now archive/frontend-next) and the legacy daily EOD job. The current way to run Stoxsy is in README.md.
#Requires -Version 5.1
<#
  Stoxsy launcher — starts the platform in separate terminal windows.

  Run from anywhere (paths resolve relative to this file's location):

    .\run.ps1                 core app: DB + backend(:8700) + frontend(:5273)
    .\run.ps1 all             core app + live market windows (stream + feed)
    .\run.ps1 live            live market windows only (Kite must be logged in)
    .\run.ps1 live -Symbols RELIANCE,TCS,KEI    live feed on specific names
    .\run.ps1 live -Top 15    live feed on today's top-15 ranked names
    .\run.ps1 stop            stop the database containers
    .\run.ps1 status          docker + backend health
    .\run.ps1 test            backend test suite (foreground)
    .\run.ps1 eod             end-of-day data refresh (foreground)
    .\run.ps1 retrain         retrain ML models, net-DSR gated (foreground)

  Daily during market hours, log in to Kite ONCE (token expires ~06:00 IST):
    http://localhost:8700/api/kite/login_url
#>
param(
  [ValidateSet("app", "all", "live", "stop", "status", "test", "eod", "retrain")]
  [string]$Mode = "app",
  [string]$Symbols = "",
  [int]$Top = 0
)

$root     = $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py       = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
  Write-Host "Python venv not found at $py" -ForegroundColor Red
  Write-Host "Create it first:  python -m venv .venv ; .\.venv\Scripts\python -m pip install -r backend\requirements.txt" -ForegroundColor Yellow
  return
}

function Open-Window($title, $cmd) {
  Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = '$title'; $cmd"
  ) | Out-Null
}

function Start-Db {
  Write-Host "[stoxsy] starting TimescaleDB + Redis (docker compose up -d)..." -ForegroundColor Cyan
  Push-Location $root
  try { docker compose up -d } catch { Write-Host "  docker not available: $_" -ForegroundColor Red }
  Pop-Location
}

switch ($Mode) {
  "stop" {
    Push-Location $root; docker compose down; Pop-Location
    Write-Host "[stoxsy] DB stopped. Close any backend/frontend/live windows manually." -ForegroundColor Yellow
    return
  }
  "status" {
    Push-Location $root; docker compose ps; Pop-Location
    try {
      $h = Invoke-WebRequest "http://localhost:8700/api/health" -TimeoutSec 3 -UseBasicParsing
      Write-Host "[stoxsy] backend :8700 -> HTTP $($h.StatusCode)" -ForegroundColor Green
    } catch { Write-Host "[stoxsy] backend :8700 -> down" -ForegroundColor Yellow }
    return
  }
  "test"    { Push-Location $backend; & $py -m pytest -q; Pop-Location; return }
  "eod"     { Push-Location $backend; & $py "scripts\daily_eod.py"; Pop-Location; return }
  "retrain" { Push-Location $backend; & $py "scripts\retrain.py"; Pop-Location; return }
}

if ($Mode -eq "app" -or $Mode -eq "all") {
  Start-Db
  Open-Window "stoxsy-backend"  "cd '$backend'; & '$py' -m uvicorn api.main:app --reload --port 8700"
  Open-Window "stoxsy-frontend" "cd '$frontend'; npm run dev"
  Write-Host ""
  Write-Host "[stoxsy] API   -> http://localhost:8700/docs" -ForegroundColor Green
  Write-Host "[stoxsy] UI    -> http://localhost:5273"       -ForegroundColor Green
  Write-Host "[stoxsy] Kite  -> log in once (market hours): http://localhost:8700/api/kite/login_url" -ForegroundColor Green
}

if ($Mode -eq "live" -or $Mode -eq "all") {
  $lw = "scripts\live_watch.py"
  if ($Symbols)        { $lw += " --symbols $Symbols" }
  elseif ($Top -gt 0)  { $lw += " --top $Top" }
  Open-Window "stoxsy-kite-stream" "cd '$backend'; & '$py' scripts\kite_stream.py"
  Start-Sleep -Seconds 2
  Open-Window "stoxsy-live-feed"   "cd '$backend'; & '$py' $lw"
  Write-Host "[stoxsy] live windows launched (needs market hours + Kite login)." -ForegroundColor Green
}
