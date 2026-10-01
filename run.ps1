# Starts Acoustic Guard. Run .\setup.ps1 once before the first start.
# native tools (pip, py) write progress to stderr; failures are caught through exit codes
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$venvPy = Join-Path $PSScriptRoot "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy) -or -not (Test-Path ".env")) {
    Write-Host "Acoustic Guard is not set up yet. Run this first:"
    Write-Host "    .\setup.ps1"
    exit 1
}
& $venvPy -c @"
import socket
from app.config import get_settings
s = get_settings()
scheme = 'https' if s.ssl_certfile else 'http'
hosts = ['127.0.0.1']
if s.host not in ('127.0.0.1', 'localhost'):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        try:
            u.connect(('10.255.255.255', 1)); hosts.append(u.getsockname()[0])
        except OSError:
            pass
print('Acoustic Guard is starting (press Ctrl+C to stop)')
for h in hosts:
    print(f'  dashboard: {scheme}://{h}:{s.port}')
if s.api_token:
    print(f'  access token: {s.api_token}')
"@
& $venvPy -m app
