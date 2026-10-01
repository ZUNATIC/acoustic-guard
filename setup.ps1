# One-time setup for Windows: environment, packages, models and configuration.
#
#   .\setup.ps1           standard install (~2.3 GB of models, includes the second-opinion model)
#   .\setup.ps1 -Light    for machines with 8 GB RAM or less (~0.7 GB of models)
#   .\setup.ps1 -Lan      also make the dashboard reachable from other computers (HTTPS + token)
#
# If scripts are blocked, first run:  Set-ExecutionPolicy -Scope Process Bypass
param([switch]$Light, [switch]$Lan)
# native tools (pip, py) write progress to stderr; failures are caught through exit codes
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$Root = (Get-Location).Path
$profileName = if ($Light) { "light" } else { "standard" }

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host "`nSetup stopped: $text" -ForegroundColor Red; exit 1 }

Step "1/5  Checking Python"
$py = $null
$candidates = @(
    @{ Exe = "py"; Args = @("-3") },
    @{ Exe = "python"; Args = @() }
)
foreach ($c in $candidates) {
    if (-not (Get-Command $c.Exe -ErrorAction SilentlyContinue)) { continue }
    $cargs = $c.Args
    & $c.Exe @cargs -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>$null
    if ($LASTEXITCODE -eq 0) { $py = $c; break }
}
if (-not $py) { Fail "Python 3.10 or newer is required. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')." }
$pyExe = $py.Exe; $pyArgs = $py.Args
Write-Host ("using " + (& $pyExe @pyArgs --version))

Step "2/5  Python environment and packages"
if (-not (Test-Path "venv\Scripts\python.exe")) { & $pyExe @pyArgs -m venv venv }
$venvPy = Join-Path $Root "venv\Scripts\python.exe"
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r requirements.txt
if ($LASTEXITCODE -ne 0) { Fail "package installation failed (check the internet connection and run .\setup.ps1 again)" }
if ($Lan) { & $venvPy -m pip install --quiet cryptography }
Write-Host "packages installed (the microphone library ships with them on Windows)"

Step "3/5  Speech models (one-time download, profile: $profileName)"
& $venvPy scripts\fetch_models.py --profile $profileName
if ($LASTEXITCODE -ne 0) { Fail "model download failed (run .\setup.ps1 again to resume)" }

Step "4/5  Configuration"
$confArgs = @()
if ($Light) { $confArgs += "--light" }
if ($Lan) { $confArgs += "--lan" }
& $venvPy scripts\configure.py @confArgs
if ($LASTEXITCODE -ne 0) { Fail "configuration failed" }

Step "5/5  Checking the installation"
& $venvPy -c "import app.main"
if ($LASTEXITCODE -ne 0) { Fail "the service does not start - see the error above" }
& $venvPy scripts\fetch_models.py --check --profile $profileName | Out-Null
if ($LASTEXITCODE -ne 0) { Fail "some models are missing - run .\setup.ps1 again" }
Write-Host "everything is in place"

Write-Host "`nSetup complete." -ForegroundColor Green
Write-Host "`nTo start Acoustic Guard, run:`n"
Write-Host "    cd `"$Root`"; .\run.ps1`n"
if ($Lan) { Write-Host "When Windows Firewall asks, allow Python on private networks so other computers can connect." }
