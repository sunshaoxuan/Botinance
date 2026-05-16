# Botinance Cross-platform Deployment

Botinance now has a Python service manager that works on Windows, Linux, and macOS:

```bash
PYTHONPATH=src python3 -m binance_ai.service_manager start
PYTHONPATH=src python3 -m binance_ai.service_manager status
PYTHONPATH=src python3 -m binance_ai.service_manager health
PYTHONPATH=src python3 -m binance_ai.service_manager stop
```

It manages two long-running processes:

- `monitor`: `binance_ai.main --loop`
- `dashboard`: `binance_ai.dashboard_server`

Runtime files are written to `runtime_visual/` by default:

- `monitor.pid`
- `dashboard.pid`
- `monitor.log`
- `dashboard.log`
- `latest_report.json`
- `cycle_reports.jsonl`
- `paper_state.json`

## Windows

Manual start:

```powershell
.\run_visual_dashboard.ps1
```

Manual status and health:

```powershell
.\boti_status.ps1
.\boti_health.ps1
```

Recommended 24-hour mode:

- Use Windows Task Scheduler, NSSM, or WinSW.
- Run from the repo root.
- Start command:
  `python -m binance_ai.service_manager start --output-dir runtime_visual`
- Set environment variable:
  `PYTHONPATH=src`
- Configure restart on failure.
- Disable system sleep on the host.

If encrypted secrets are enabled on Windows, provide `BINANCE_AI_SECRETS_PASSPHRASE` through the service environment until a Windows Credential Manager/DPAPI backend is added.

## Linux

Manual start:

```bash
./run_visual_dashboard.sh
```

Recommended `systemd` service command:

```ini
ExecStart=/usr/bin/python3 -m binance_ai.service_manager start --output-dir runtime_visual
WorkingDirectory=/path/to/binance-ai
Environment=PYTHONPATH=src
Restart=always
RestartSec=10
```

Use `boti_health.sh` from a timer or external monitor to detect stale reports.

## macOS

Manual start:

```bash
./run_visual_dashboard.sh
```

For 24-hour operation on macOS, use `launchd` with `KeepAlive=true`, but a non-sleeping Windows/Linux host is a better target for continuous operation.

## Health Criteria

`health` fails when any of these are true:

- dashboard process is not running
- dashboard HTTP API is unreachable
- monitor process is not running
- `latest_report.json` is missing or older than `BOTI_STALE_SECONDS`

Default stale threshold:

```text
BOTI_STALE_SECONDS=180
```
