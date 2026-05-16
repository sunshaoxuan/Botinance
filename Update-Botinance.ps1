param(
  [string]$Branch = $(if ($env:BOTI_GIT_BRANCH) { $env:BOTI_GIT_BRANCH } else { "main" }),
  [string]$Remote = $(if ($env:BOTI_GIT_REMOTE) { $env:BOTI_GIT_REMOTE } else { "origin" }),
  [string]$OutputDir = $(if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "runtime_visual" }),
  [string]$HostAddress = $(if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "0.0.0.0" }),
  [int]$Port = $(if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 }),
  [int]$SleepSeconds = $(if ($env:SLEEP_SECONDS) { [int]$env:SLEEP_SECONDS } else { 3 }),
  [int]$StaleSeconds = $(if ($env:BOTI_STALE_SECONDS) { [int]$env:BOTI_STALE_SECONDS } else { 180 })
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Git = $(if ($env:GIT_EXE) { $env:GIT_EXE } else { "git" })
$Python = $(if ($env:PYTHON_EXE) { $env:PYTHON_EXE } else { "python" })
$LogDir = Join-Path $RootDir $OutputDir
$LogPath = Join-Path $LogDir "git_sync.log"

function Write-Log {
  param([string]$Message)
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $LogPath -Value "$timestamp $Message"
  Write-Output $Message
}

function Invoke-InRepo {
  param([string[]]$Arguments)
  & $Git -C $RootDir @Arguments
}

function Invoke-Start {
  $startScript = Join-Path $RootDir "Start-Botinance.ps1"
  if (Test-Path $startScript) {
    & $startScript
    return
  }
  $env:PYTHONPATH = "src"
  & $Python -m binance_ai.service_manager start `
    --output-dir $OutputDir `
    --sleep-seconds $SleepSeconds `
    --host $HostAddress `
    --port $Port
}

function Invoke-Stop {
  $stopScript = Join-Path $RootDir "Stop-Botinance.ps1"
  if (Test-Path $stopScript) {
    & $stopScript
    return
  }
  $env:PYTHONPATH = "src"
  & $Python -m binance_ai.service_manager stop `
    --output-dir $OutputDir `
    --host $HostAddress `
    --port $Port
}

function Test-Healthy {
  $env:PYTHONPATH = "src"
  & $Python -m binance_ai.service_manager health `
    --output-dir $OutputDir `
    --host "127.0.0.1" `
    --port $Port `
    --stale-seconds $StaleSeconds | Out-Null
}

Set-Location $RootDir

try {
  Write-Log "git-sync check branch=$Branch"
  Invoke-InRepo @("fetch", "--prune", $Remote, $Branch) | Out-Null
  $localHead = (Invoke-InRepo @("rev-parse", "HEAD")).Trim()
  $remoteHead = (Invoke-InRepo @("rev-parse", "$Remote/$Branch")).Trim()

  if ($localHead -ne $remoteHead) {
    Write-Log "update detected local=$($localHead.Substring(0, 7)) remote=$($remoteHead.Substring(0, 7))"
    Invoke-Stop | Out-Null
    Invoke-InRepo @("reset", "--hard", "$Remote/$Branch") | Out-Null
    Invoke-Start | Out-Null
    Write-Log "updated and restarted head=$($remoteHead.Substring(0, 7))"
    exit 0
  }

  try {
    Test-Healthy
    Write-Log "no update; service healthy head=$($localHead.Substring(0, 7))"
  } catch {
    Write-Log "no update; health failed, restarting: $($_.Exception.Message)"
    Invoke-Start | Out-Null
  }
} catch {
  Write-Log "git-sync failed: $($_.Exception.Message)"
  exit 1
}
