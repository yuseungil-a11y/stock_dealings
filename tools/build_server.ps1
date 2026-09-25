# stock_svr Windows exe build + deploy script (PyInstaller, onedir)
#   powershell -ExecutionPolicy Bypass -File tools\build_server.ps1 [-WithLocalConfig] [-StopRunning] [-Launch] [-SkipTests] [-RunDir D:\claude_stock_run]
#
# Build output (intermediate) : server\dist\stock_svr\
# Run folder (deployed)       : D:\claude_stock_run   (separate from the source tree)
#   stock_svr.exe (GUI only - no CLI exe), _internal\, config\, logs\
#
# Notes  : - Redeploy keeps the run folder's config\ and logs\ (only exe + _internal are replaced).
#          - -WithLocalConfig copies server\config\config.local.ini (contains DB password) and
#            server\config\mail.local.json (contains SMTP password, for trade-completed email
#            notifications) into <RunDir>\config and restricts both ACLs. Without it the existing
#            run-folder config is kept.
#          - Keys are read from the key-file paths written in config.local.ini (never bundled).
#          - -StopRunning stops a stock_svr.exe running from <RunDir> before replacing files
#            (without it the script aborts if the server is running).
#          - -Launch starts <RunDir>\stock_svr.exe after deployment.
param(
  [string]$RunDir = 'D:\claude_stock_run',
  [switch]$WithLocalConfig,
  [switch]$StopRunning,
  [switch]$Launch,
  [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$root   = Split-Path -Parent $PSScriptRoot
$server = Join-Path $root 'server'
$dist   = Join-Path $server 'dist\stock_svr'
Set-Location $server

# --- safety: RunDir sanity ---------------------------------------------------
$RunDir = [System.IO.Path]::GetFullPath($RunDir).TrimEnd('\')
if ($RunDir.Length -lt 6 -or $RunDir -eq [System.IO.Path]::GetPathRoot($RunDir).TrimEnd('\')) { throw "unsafe RunDir: $RunDir" }
if ($RunDir.StartsWith($server, [StringComparison]::OrdinalIgnoreCase)) { throw 'RunDir must be outside the source tree (server\)' }
if ((Test-Path $RunDir) -and (Get-ChildItem $RunDir -Force | Measure-Object).Count -gt 0 -and
    -not (Test-Path (Join-Path $RunDir 'stock_svr.exe')) -and -not (Test-Path (Join-Path $RunDir '_internal'))) {
  throw "RunDir is not empty and does not look like a stock_svr run folder: $RunDir"
}

$ver = (python -c "import stock_svr; print(stock_svr.__version__)").Trim()
if ($ver -notmatch '^\d+\.\d+\.\d+$') { throw "cannot read server version (got '$ver')" }
Write-Host "stock_svr version: v$ver  (bump with: python tools\bump_version.py server patch|minor|major)"

if (-not $SkipTests) {
  Write-Host '[1/5] pytest ...'
  python -m pytest -q
  if ($LASTEXITCODE -ne 0) { throw 'pytest failed - build aborted' }
} else { Write-Host '[1/5] tests skipped' }

Write-Host '[2/5] PyInstaller ...'
python -m pip install --quiet -r requirements-build.txt
if (Test-Path build) { Remove-Item build -Recurse -Force }
if (Test-Path dist)  { Remove-Item dist  -Recurse -Force }
python -m PyInstaller --noconfirm --clean stock_svr.spec
if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed' }
if (-not (Test-Path (Join-Path $dist 'stock_svr.exe'))) { throw 'build output missing stock_svr.exe' }

Write-Host "[3/5] stop running server (if any) ..."
$running = @(Get-Process stock_svr -ErrorAction SilentlyContinue | Where-Object { $_.Path -and $_.Path.StartsWith($RunDir, [StringComparison]::OrdinalIgnoreCase) })
if ($running.Count -gt 0) {
  if (-not $StopRunning) { throw "stock_svr.exe is running from $RunDir (PID $($running.Id -join ',')). Close it or use -StopRunning." }
  $running | Stop-Process -Force
  Start-Sleep -Seconds 2
  Write-Host "      stopped PID $($running.Id -join ',')"
}

Write-Host "[4/5] deploy -> $RunDir ..."
New-Item -ItemType Directory -Force $RunDir, (Join-Path $RunDir 'config'), (Join-Path $RunDir 'logs') | Out-Null
# mirror exe + _internal only; keep config\ and logs\ of the run folder untouched
robocopy $dist $RunDir /MIR /XD config logs /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed (exit $LASTEXITCODE)" }
$global:LASTEXITCODE = 0
Copy-Item (Join-Path $server 'config\config.example.ini') (Join-Path $RunDir 'config') -Force
Copy-Item (Join-Path $server 'config\mail.example.json') (Join-Path $RunDir 'config') -Force
$runCfg = Join-Path $RunDir 'config\config.local.ini'
$runMailCfg = Join-Path $RunDir 'config\mail.local.json'
if ($WithLocalConfig) {
  $local = Join-Path $server 'config\config.local.ini'
  if (Test-Path $local) {
    Copy-Item $local $runCfg -Force
    icacls $runCfg /inheritance:r /grant:r "${env:USERNAME}:(F)" 'BUILTIN\Administrators:(F)' 'NT AUTHORITY\SYSTEM:(F)' | Out-Null
    Write-Host '      config.local.ini copied (ACL restricted)'
  } else { Write-Warning 'server\config\config.local.ini not found - skipped' }
  $localMail = Join-Path $server 'config\mail.local.json'
  if (Test-Path $localMail) {
    Copy-Item $localMail $runMailCfg -Force
    icacls $runMailCfg /inheritance:r /grant:r "${env:USERNAME}:(F)" 'BUILTIN\Administrators:(F)' 'NT AUTHORITY\SYSTEM:(F)' | Out-Null
    Write-Host '      mail.local.json copied (ACL restricted)'
  } else { Write-Warning 'server\config\mail.local.json not found - skipped' }
}
if (-not (Test-Path $runCfg)) { Write-Warning "no config\config.local.ini in $RunDir - create it (see config.example.ini) or re-run with -WithLocalConfig" }
if (-not (Test-Path $runMailCfg)) { Write-Warning "no config\mail.local.json in $RunDir - trade-completed email notifications disabled (see mail.example.json) or re-run with -WithLocalConfig" }

Write-Host '[5/5] verify ...'
$exe = Join-Path $RunDir 'stock_svr.exe'
if (-not (Test-Path $exe)) { throw 'missing stock_svr.exe in run folder' }
'{0}  {1:N1} MB  ({2})' -f 'stock_svr.exe', ((Get-Item $exe).Length / 1MB), (Get-Item $exe).LastWriteTime
# record what is deployed (version, build time, git commit if available)
$commit = 'n/a'
try { $c = (git -C $root rev-parse --short HEAD 2>$null); if ($LASTEXITCODE -eq 0 -and $c) { $commit = $c.Trim() } } catch { }
$global:LASTEXITCODE = 0
@("stock_svr v$ver", "built  $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')", "commit $commit") | Set-Content -Path (Join-Path $RunDir 'VERSION.txt') -Encoding ASCII
Write-Host "Deploy OK -> $RunDir   (stock_svr v$ver, commit $commit)"
if ($Launch) {
  Start-Process -FilePath $exe -WorkingDirectory $RunDir | Out-Null
  Write-Host "Launched $exe"
} else {
  Write-Host "Run: $exe   (diagnostics: server\run_stock_svr.bat --check)"
}
