from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List
from urllib.error import URLError
from urllib.request import urlopen


DEFAULT_OUTPUT_DIR = "runtime_visual"
DEFAULT_SLEEP_SECONDS = 3
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STALE_REPORT_SECONDS = 180


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _pid_path(output_dir: Path, name: str) -> Path:
    return output_dir / f"{name}.pid"


def _log_path(output_dir: Path, name: str) -> Path:
    return output_dir / f"{name}.log"


def _read_pid(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n", encoding="utf-8")


def _is_process_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return f'"{pid}"' in result.stdout or f",{pid}," in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _stop_process(pid: int | None, timeout_seconds: float = 8.0) -> bool:
    if not _is_process_running(pid):
        return False
    assert pid is not None
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T"], capture_output=True, text=True, check=False)
    else:
        os.kill(pid, signal.SIGTERM)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if not _is_process_running(pid):
            return True
        time.sleep(0.2)
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, text=True, check=False)
    else:
        os.kill(pid, signal.SIGKILL)
    return True


def _python_env(root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    src_path = str(root / "src")
    current = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_path if not current else os.pathsep.join([src_path, current])
    return env


def _popen_detached(command: List[str], *, root: Path, output_dir: Path, log_name: str) -> subprocess.Popen[Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = _log_path(output_dir, log_name).open("ab")
    kwargs: Dict[str, Any] = {
        "cwd": str(root),
        "env": _python_env(root),
        "stdin": subprocess.DEVNULL,
        "stdout": log_file,
        "stderr": subprocess.STDOUT,
        "close_fds": os.name != "nt",
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(command, **kwargs)


def _port_is_listening(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _http_ok(url: str, timeout: float = 5.0) -> bool:
    try:
        with urlopen(url, timeout=timeout) as response:  # noqa: S310 - local health check URL.
            return 200 <= response.status < 500
    except (OSError, URLError):
        return False


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_report_age_seconds(output_dir: Path) -> float | None:
    path = output_dir / "latest_report.json"
    if not path.exists():
        return None
    return max(0.0, time.time() - path.stat().st_mtime)


def _process_status(output_dir: Path, name: str) -> Dict[str, Any]:
    pid = _read_pid(_pid_path(output_dir, name))
    return {
        "pid": pid,
        "running": _is_process_running(pid),
        "pid_file": str(_pid_path(output_dir, name)),
        "log_file": str(_log_path(output_dir, name)),
    }


def start_services(args: argparse.Namespace) -> Dict[str, Any]:
    root = _repo_root()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {"output_dir": str(output_dir)}

    if args.stop_existing:
        stop_services(args)

    monitor_pid = _read_pid(_pid_path(output_dir, "monitor"))
    if not _is_process_running(monitor_pid):
        monitor = _popen_detached(
            [
                sys.executable,
                "-u",
                "-m",
                "binance_ai.main",
                "--loop",
                "--sleep-seconds",
                str(args.sleep_seconds),
                "--output-dir",
                str(output_dir),
            ],
            root=root,
            output_dir=output_dir,
            log_name="monitor",
        )
        _write_pid(_pid_path(output_dir, "monitor"), monitor.pid)
        result["monitor_started"] = monitor.pid
    else:
        result["monitor_running"] = monitor_pid

    dashboard_pid = _read_pid(_pid_path(output_dir, "dashboard"))
    if not _is_process_running(dashboard_pid):
        dashboard = _popen_detached(
            [
                sys.executable,
                "-u",
                "-m",
                "binance_ai.dashboard_server",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--output-dir",
                str(output_dir),
            ],
            root=root,
            output_dir=output_dir,
            log_name="dashboard",
        )
        _write_pid(_pid_path(output_dir, "dashboard"), dashboard.pid)
        result["dashboard_started"] = dashboard.pid
    else:
        result["dashboard_running"] = dashboard_pid

    result["url"] = f"http://{args.host}:{args.port}"
    return result


def stop_services(args: argparse.Namespace) -> Dict[str, Any]:
    output_dir = Path(args.output_dir)
    result: Dict[str, Any] = {"output_dir": str(output_dir), "stopped": []}
    for name in ("monitor", "dashboard"):
        path = _pid_path(output_dir, name)
        pid = _read_pid(path)
        if _stop_process(pid):
            result["stopped"].append({"name": name, "pid": pid})
        path.unlink(missing_ok=True)
    return result


def status_services(args: argparse.Namespace) -> Dict[str, Any]:
    output_dir = Path(args.output_dir)
    latest_report = _load_json(output_dir / "latest_report.json")
    age = _latest_report_age_seconds(output_dir)
    return {
        "output_dir": str(output_dir),
        "dashboard": _process_status(output_dir, "dashboard"),
        "monitor": _process_status(output_dir, "monitor"),
        "dashboard_url": f"http://{args.host}:{args.port}",
        "dashboard_port_listening": _port_is_listening(args.host, args.port),
        "latest_report_age_seconds": age,
        "latest_report_stale": age is None or age > args.stale_seconds,
        "latest_report_timestamp_ms": latest_report.get("timestamp_ms"),
        "latest_cycle_mode": latest_report.get("cycle_mode"),
        "latest_market_prices": latest_report.get("market_prices", {}),
    }


def health_services(args: argparse.Namespace) -> Dict[str, Any]:
    status = status_services(args)
    dashboard_ok = bool(status["dashboard"]["running"]) and _http_ok(f"http://{args.host}:{args.port}/api/dashboard?include_chart=false")
    monitor_ok = bool(status["monitor"]["running"]) and not bool(status["latest_report_stale"])
    status["healthy"] = dashboard_ok and monitor_ok
    status["dashboard_http_ok"] = dashboard_ok
    status["monitor_fresh"] = monitor_ok
    return status


def restart_services(args: argparse.Namespace) -> Dict[str, Any]:
    stopped = stop_services(args)
    time.sleep(1)
    started = start_services(args)
    return {"stopped": stopped, "started": started}


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Manage Botinance local services.")
    parser.add_argument("command", choices=["start", "stop", "restart", "status", "health"])
    parser.add_argument("--output-dir", default=os.getenv("OUTPUT_DIR", DEFAULT_OUTPUT_DIR))
    parser.add_argument("--sleep-seconds", type=int, default=int(os.getenv("SLEEP_SECONDS", str(DEFAULT_SLEEP_SECONDS))))
    parser.add_argument("--host", default=os.getenv("DASHBOARD_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", str(DEFAULT_PORT))))
    parser.add_argument("--stale-seconds", type=int, default=int(os.getenv("BOTI_STALE_SECONDS", str(STALE_REPORT_SECONDS))))
    parser.add_argument("--stop-existing", action="store_true", default=True)
    parser.add_argument("--no-stop-existing", action="store_false", dest="stop_existing")
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> None:
    args = parse_args(argv)
    if args.command == "start":
        payload = start_services(args)
    elif args.command == "stop":
        payload = stop_services(args)
    elif args.command == "restart":
        payload = restart_services(args)
    elif args.command == "status":
        payload = status_services(args)
    else:
        payload = health_services(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.command == "health" and not payload.get("healthy"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
