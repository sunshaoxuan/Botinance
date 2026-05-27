param(
  [string]$OutputDir = $(if ($env:OUTPUT_DIR) { $env:OUTPUT_DIR } else { "runtime_visual" }),
  [string]$HostAddress = $(if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "0.0.0.0" }),
  [int]$Port = $(if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 }),
  [int]$SleepSeconds = $(if ($env:SLEEP_SECONDS) { [int]$env:SLEEP_SECONDS } else { 3 })
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
  & docker info *> $null
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
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) {
          break
        }
      } while ((Get-Date) -lt $deadline)
    }
  }
  & docker info *> $null
  if ($LASTEXITCODE -ne 0) {
    Write-StartLog "docker engine is not ready; Botinance will keep file fallback"
    return $false
  }
  try {
    Write-StartLog "starting PostgreSQL container"
    & docker compose -f (Join-Path $RootDir "docker-compose.yml") up -d postgres | Out-Null
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

function Import-RuntimeToPostgres {
  param([string]$Python)
  Write-StartLog "migrating runtime JSON data into PostgreSQL"
  $env:PYTHONPATH = "src"
  & $Python -m binance_ai.storage.migrate_runtime `
    --runtime-dir $OutputDir `
    --database-url "postgresql://$($env:DB_USER):$($env:BOTINANCE_DB_PASSWORD)@$($env:DB_HOST):$($env:DB_PORT)/$($env:DB_NAME)" | Out-Null
  if ($LASTEXITCODE -ne 0) {
    Write-StartLog "runtime migration failed; Botinance can still fall back to files"
  }
}

Set-Location $RootDir
$PythonExe = Resolve-Python
Ensure-DbPassword
Enable-PostgresRuntimeEnv
Ensure-PythonPostgresDriver -Python $PythonExe
$postgresReady = Start-PostgresContainer
if ($postgresReady) {
  Import-RuntimeToPostgres -Python $PythonExe
} else {
  $env:DB_WRITE_MODE = "file"
  $env:DB_READ_MODE = "file"
}

$env:PYTHONPATH = "src"
& $PythonExe -m binance_ai.service_manager start `
  --output-dir $OutputDir `
  --sleep-seconds $SleepSeconds `
  --host $HostAddress `
  --port $Port
