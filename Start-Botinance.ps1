param(
  [string]$OutputDir = $(if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "runtime_visual" }),
  [string]$HostAddress = $(if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "0.0.0.0" }),
  [int]$Port = $(if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 }),
  [int]$SleepSeconds = $(if ($env:SLEEP_SECONDS) { [int]$env:SLEEP_SECONDS } else { 3 }),
  [int]$MigrationTimeoutSeconds = $(if ($env:BOTI_MIGRATION_TIMEOUT_SECONDS) { [int]$env:BOTI_MIGRATION_TIMEOUT_SECONDS } else { 60 }),
  [switch]$RunMigration = $([string]::Equals($env:BOTI_RUN_RUNTIME_MIGRATION, "true", [System.StringComparison]::OrdinalIgnoreCase))
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDir = Join-Path $RootDir $OutputDir
$LogPath = Join-Path $LogDir "start_botinance.log"

function Write-StartLog {
  param([string]$Message)
  New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
  $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
  Add-Content -Path $LogPath -Value "$timestamp $Message"
  Write-Output $Message
}

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

function Ensure-DbPassword {
  if (-not [string]::IsNullOrWhiteSpace($env:BOTINANCE_DB_PASSWORD)) {
    return
  }
  $existing = [Environment]::GetEnvironmentVariable("BOTINANCE_DB_PASSWORD", "User")
  if (-not [string]::IsNullOrWhiteSpace($existing)) {
    $env:BOTINANCE_DB_PASSWORD = $existing
    return
  }
  $bytes = New-Object byte[] 24
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try {
    $rng.GetBytes($bytes)
  } finally {
    $rng.Dispose()
  }
  $generated = [Convert]::ToBase64String($bytes).TrimEnd("=")
  [Environment]::SetEnvironmentVariable("BOTINANCE_DB_PASSWORD", $generated, "User")
  $env:BOTINANCE_DB_PASSWORD = $generated
  Write-StartLog "generated user-level BOTINANCE_DB_PASSWORD"
}

function Enable-PostgresRuntimeEnv {
  $env:DB_ENABLED = "true"
  $env:DB_DRIVER = "postgres"
  $env:DB_HOST = $(if ($env:DB_HOST) { $env:DB_HOST } else { "127.0.0.1" })
  $env:DB_PORT = $(if ($env:DB_PORT) { $env:DB_PORT } else { "5432" })
  $env:DB_NAME = $(if ($env:DB_NAME) { $env:DB_NAME } else { "botinance" })
  $env:DB_USER = $(if ($env:DB_USER) { $env:DB_USER } else { "botinance" })
  $env:DB_PASSWORD_ENV = "BOTINANCE_DB_PASSWORD"
  $env:DB_WRITE_MODE = "dual"
  $env:DB_READ_MODE = "prefer_db"
  $env:DASHBOARD_ORDER_SOURCE = "postgres"
  $env:DB_FALLBACK_TO_FILE = $(if ($env:DB_FALLBACK_TO_FILE) { $env:DB_FALLBACK_TO_FILE } else { "false" })
}

function Ensure-PythonPostgresDriver {
  param([string]$Python)
  & $Python -c "import psycopg" 2>$null
  if ($LASTEXITCODE -eq 0) {
    return
  }
  Write-StartLog "installing psycopg binary driver"
  & $Python -m pip install "psycopg[binary]>=3.1" | Out-Null
}

function Start-PostgresContainer {
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    Write-StartLog "docker command not found; Botinance will keep file fallback"
    return $false
  }
  & cmd.exe /c "docker info >NUL 2>NUL"
  if ($LASTEXITCODE -ne 0) {
    $dockerDesktop = @(
      "C:\Program Files\Docker\Docker\Docker Desktop.exe",
      "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
    ) | Where-Object { Test-Path $_ } | Select-Object -First 1
    if ($dockerDesktop) {
      Write-StartLog "starting Docker Desktop"
      Start-Process -FilePath $dockerDesktop | Out-Null
      $deadline = (Get-Date).AddSeconds(90)
      do {
        Start-Sleep -Seconds 3
        & cmd.exe /c "docker info >NUL 2>NUL"
        if ($LASTEXITCODE -eq 0) {
          break
        }
      } while ((Get-Date) -lt $deadline)
    }
  }
  & cmd.exe /c "docker info >NUL 2>NUL"
  if ($LASTEXITCODE -ne 0) {
    Write-StartLog "docker engine is not ready; Botinance will keep file fallback"
    return $false
  }
  try {
    Write-StartLog "starting PostgreSQL container"
    $composeFile = Join-Path $RootDir "docker-compose.yml"
    & cmd.exe /c "docker compose -f `"$composeFile`" up -d postgres >NUL 2>NUL"
    if ($LASTEXITCODE -ne 0) {
      Write-StartLog "docker compose failed with exit code $LASTEXITCODE; Botinance will keep file fallback"
      return $false
    }
    return $true
  } catch {
    Write-StartLog "docker compose failed: $($_.Exception.Message); Botinance will keep file fallback"
    return $false
  }
}

function Sync-PostgresPassword {
  if ([string]::IsNullOrWhiteSpace($env:BOTINANCE_DB_PASSWORD)) {
    Write-StartLog "BOTINANCE_DB_PASSWORD is empty; PostgreSQL password sync skipped"
    return $false
  }
  $docker = Get-Command docker -ErrorAction SilentlyContinue
  if (-not $docker) {
    return $false
  }
  try {
    $escapedPassword = $env:BOTINANCE_DB_PASSWORD.Replace("'", "''")
    $sql = "ALTER USER botinance PASSWORD '$escapedPassword';"
    & docker exec botinance-postgres psql -U botinance -d botinance -c $sql | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-StartLog "PostgreSQL password sync failed with exit code $LASTEXITCODE"
      return $false
    }
    Write-StartLog "PostgreSQL password synced with BOTINANCE_DB_PASSWORD"
    return $true
  } catch {
    Write-StartLog "PostgreSQL password sync failed: $($_.Exception.Message)"
    return $false
  }
}

function Import-RuntimeToPostgres {
  param([string]$Python)
  Write-StartLog "migrating runtime JSON data into PostgreSQL"
  $env:PYTHONPATH = "src"
  $scriptBlock = {
    param($RootDir, $Python, $OutputDir)
    Set-Location $RootDir
    $env:PYTHONPATH = "src"
    & $Python -m binance_ai.storage.migrate_runtime --runtime-dir $OutputDir
  }
  $job = Start-Job -ScriptBlock $scriptBlock -ArgumentList $RootDir, $Python, $OutputDir
  if (Wait-Job $job -Timeout $MigrationTimeoutSeconds) {
    Receive-Job $job | Out-Null
    $state = $job.State
    Remove-Job $job
    if ($state -ne "Completed") {
      Write-StartLog "runtime migration ended with state=$state; Botinance can still fall back to files"
    }
  } else {
    Stop-Job $job -ErrorAction SilentlyContinue
    Remove-Job $job -Force -ErrorAction SilentlyContinue
    Write-StartLog "runtime migration timed out after ${MigrationTimeoutSeconds}s; continuing startup"
  }
}

function Invoke-MaintenanceCommand {
  param([string]$Python)
  $commandPath = Join-Path $RootDir "ops\maintenance_command.json"
  if (-not (Test-Path $commandPath)) {
    return
  }
  $statePath = Join-Path $LogDir "maintenance_state.json"
  try {
    $command = Get-Content -Raw -Path $commandPath | ConvertFrom-Json
  } catch {
    Write-StartLog "maintenance command parse failed: $($_.Exception.Message)"
    return
  }
  $commandId = [string]$command.id
  $commandType = [string]$command.type
  if ([string]::IsNullOrWhiteSpace($commandId) -or [string]::IsNullOrWhiteSpace($commandType)) {
    Write-StartLog "maintenance command ignored; id or type is empty"
    return
  }
  $state = @{}
  if (Test-Path $statePath) {
    try {
      $state = Get-Content -Raw -Path $statePath | ConvertFrom-Json
    } catch {
      $state = @{}
    }
  }
  $applied = @()
  if ($state.applied_ids) {
    $applied = @($state.applied_ids)
  }
  if ($applied -contains $commandId) {
    Write-StartLog "maintenance command already applied id=$commandId"
    return
  }
  if ($commandType -ne "reset_paper_from_real_account") {
    Write-StartLog "maintenance command ignored; unsupported type=$commandType"
    return
  }

  Write-StartLog "applying maintenance command id=$commandId type=$commandType"
  $env:PYTHONPATH = "src"
  $args = @(
    "-m", "binance_ai.tools.sync_paper_from_account",
    "--output-dir", $OutputDir,
    "--archive-root", $(if ($command.archive_root) { [string]$command.archive_root } else { "runtime_resets" })
  )
  if ($command.cash_baseline -eq $true) {
    $args += "--cash-baseline"
  }
  if ($command.min_cash_baseline) {
    $args += @("--min-cash-baseline", [string]$command.min_cash_baseline)
  }
  if ($command.require_asset_min) {
    foreach ($item in @($command.require_asset_min)) {
      $args += @("--require-asset-min", [string]$item)
    }
  }
  & $Python @args | Tee-Object -FilePath (Join-Path $LogDir "maintenance_command.log") -Append | Out-Null
  if ($LASTEXITCODE -ne 0) {
    throw "maintenance command failed id=$commandId exit=$LASTEXITCODE"
  }
  $applied += $commandId
  @{
    applied_ids = $applied
    last_applied_id = $commandId
    last_applied_at = (Get-Date).ToString("s")
  } | ConvertTo-Json -Depth 6 | Set-Content -Path $statePath -Encoding UTF8
  Write-StartLog "maintenance command applied id=$commandId"
}

Set-Location $RootDir
$PythonExe = Resolve-Python
Ensure-DbPassword
Enable-PostgresRuntimeEnv
Ensure-PythonPostgresDriver -Python $PythonExe
$postgresReady = Start-PostgresContainer
if ($postgresReady) {
  Sync-PostgresPassword | Out-Null
}
if ($postgresReady -and $RunMigration) {
  Import-RuntimeToPostgres -Python $PythonExe
} elseif ($postgresReady) {
  Write-StartLog "runtime migration skipped; set BOTI_RUN_RUNTIME_MIGRATION=true to import legacy JSON"
} else {
  if ([string]::Equals($env:DB_FALLBACK_TO_FILE, "true", [System.StringComparison]::OrdinalIgnoreCase)) {
    $env:DB_WRITE_MODE = "file"
    $env:DB_READ_MODE = "file"
    Write-StartLog "PostgreSQL unavailable; explicit DB_FALLBACK_TO_FILE=true allows file mode"
  } else {
    throw "PostgreSQL is required for Botinance runtime. Start Docker/PostgreSQL or set DB_FALLBACK_TO_FILE=true explicitly."
  }
}

Invoke-MaintenanceCommand -Python $PythonExe

$env:PYTHONPATH = "src"
& $PythonExe -m binance_ai.service_manager start `
  --output-dir $OutputDir `
  --sleep-seconds $SleepSeconds `
  --host $HostAddress `
  --port $Port
