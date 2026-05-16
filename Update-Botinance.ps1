param(
  [string]$Branch = $(if ($env:BOTI_GIT_BRANCH) { $env:BOTI_GIT_BRANCH } else { "main" }),
  [string]$Remote = $(if ($env:BOTI_GIT_REMOTE) { $env:BOTI_GIT_REMOTE } else { "origin" }),
  [string]$OutputDir = $(if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "runtime_visual" }),
  [string]$HostAddress = $(if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "0.0.0.0" }),
  [int]$Port = $(if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 }),
  [int]$SleepSeconds = $(if ($env:SLEEP_SECONDS) { [int]$env:SLEEP_SECONDS } else { 3 }),
  [int]$StaleSeconds = $(if ($env:BOTI_STALE_SECONDS) { [int]$env:BOTI_STALE_SECONDS } else { 180 }),
  [int]$StopTimeoutSeconds = $(if ($env:BOTI_STOP_TIMEOUT_SECONDS) { [int]$env:BOTI_STOP_TIMEOUT_SECONDS } else { 30 }),
  [int]$StartTimeoutSeconds = $(if ($env:BOTI_START_TIMEOUT_SECONDS) { [int]$env:BOTI_START_TIMEOUT_SECONDS } else { 45 })
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Git = $(if ($env:GIT_EXE) { $env:GIT_EXE } else { "git" })
$LogDir = Join-Path $RootDir $OutputDir
$LogPath = Join-Path $LogDir "git_sync.log"

function Resolve-Python {
  if ($env:PYTHON_EXE -and (Test-Path $env:PYTHON_EXE)) {
    return $env:PYTHON_EXE
  }
  $candidates = @(
    "C:\Users\sunsx\AppData\Local\Programs\Python\Python312\python.exe",
    "C:\Users\sunsx\AppData\Local\Programs\Python\Python311\python.exe",
    "C:\Python312\python.exe",
    "C:\Python311\python.exe"
  )
  foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
      return $candidate
    }
  }
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if ($cmd) {
    return $cmd.Source
  }
  $py = Get-Command py -ErrorAction SilentlyContinue
  if ($py) {
    return $py.Source
  }
  throw "Python executable not found. Set PYTHON_EXE or install Python."
}

$Python = Resolve-Python

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

function Stop-BotinanceProcesses {
  $patterns = @("*binance_ai*", "*openssl.exe*secrets*")
  foreach ($pattern in $patterns) {
    Get-CimInstance Win32_Process |
      Where-Object { $_.CommandLine -like $pattern } |
      ForEach-Object {
        Write-Log "force stopping pid=$($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
      }
  }
}

function Invoke-Start {
  $startScript = Join-Path $RootDir "Start-Botinance.ps1"
  $scriptBlock = {
    param($RootDir, $Python, $OutputDir, $SleepSeconds, $HostAddress, $Port, $StartScript)
    Set-Location $RootDir
    if (Test-Path $StartScript) {
      & $StartScript
      return
    }
    $env:PYTHONPATH = "src"
    & $Python -m binance_ai.service_manager start `
      --output-dir $OutputDir `
      --sleep-seconds $SleepSeconds `
      --host $HostAddress `
      --port $Port
  }
  $job = Start-Job -ScriptBlock $scriptBlock -ArgumentList $RootDir, $Python, $OutputDir, $SleepSeconds, $HostAddress, $Port, $startScript
  if (Wait-Job $job -Timeout $StartTimeoutSeconds) {
    Receive-Job $job
    Remove-Job $job
  } else {
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Write-Log "start timed out after ${StartTimeoutSeconds}s; checking health"
    Test-Healthy
  }
}

function Invoke-Stop {
  $scriptBlock = {
    param($RootDir, $Python, $OutputDir, $HostAddress, $Port)
    Set-Location $RootDir
    $env:PYTHONPATH = "src"
    & $Python -m binance_ai.service_manager stop `
      --output-dir $OutputDir `
      --host $HostAddress `
      --port $Port
  }
  $job = Start-Job -ScriptBlock $scriptBlock -ArgumentList $RootDir, $Python, $OutputDir, $HostAddress, $Port
  if (Wait-Job $job -Timeout $StopTimeoutSeconds) {
    Receive-Job $job
    Remove-Job $job
  } else {
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Write-Log "graceful stop timed out after ${StopTimeoutSeconds}s"
    Stop-BotinanceProcesses
  }
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
    Stop-BotinanceProcesses
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
    Stop-BotinanceProcesses
    Invoke-Start | Out-Null
  }
} catch {
  Write-Log "git-sync failed: $($_.Exception.Message)"
  exit 1
}
