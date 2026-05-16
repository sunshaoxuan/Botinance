param(
  [string]$OutputDir = $env:OUTPUT_DIR,
  [int]$SleepSeconds = $(if ($env:SLEEP_SECONDS) { [int]$env:SLEEP_SECONDS } else { 3 }),
  [string]$HostAddress = $(if ($env:DASHBOARD_HOST) { $env:DASHBOARD_HOST } else { "127.0.0.1" }),
  [int]$Port = $(if ($env:DASHBOARD_PORT) { [int]$env:DASHBOARD_PORT } else { 8765 })
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
  $OutputDir = "runtime_visual"
}

$env:PYTHONPATH = "src"
python -m binance_ai.service_manager start `
  --output-dir $OutputDir `
  --sleep-seconds $SleepSeconds `
  --host $HostAddress `
  --port $Port
