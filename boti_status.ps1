param(
  [string]$OutputDir = $env:OUTPUT_DIR,
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
python -m binance_ai.service_manager status --output-dir $OutputDir --host $HostAddress --port $Port
