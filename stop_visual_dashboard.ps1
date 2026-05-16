param(
  [string]$OutputDir = $env:OUTPUT_DIR
)

$ErrorActionPreference = "Stop"
$RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RootDir

if ([string]::IsNullOrWhiteSpace($OutputDir)) {
  $OutputDir = "runtime_visual"
}

$env:PYTHONPATH = "src"
python -m binance_ai.service_manager stop --output-dir $OutputDir
