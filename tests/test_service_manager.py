from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from binance_ai import service_manager


class ServiceManagerTests(unittest.TestCase):
    def test_status_reports_missing_processes_and_stale_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            args = service_manager.parse_args(["status", "--output-dir", tmpdir, "--host", "127.0.0.1", "--port", "9"])

            status = service_manager.status_services(args)

        self.assertFalse(status["dashboard"]["running"])
        self.assertFalse(status["monitor"]["running"])
        self.assertTrue(status["latest_report_stale"])
        self.assertFalse(status["dashboard_port_listening"])

    def test_latest_report_age_is_none_when_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(service_manager._latest_report_age_seconds(Path(tmpdir)))

    def test_pid_file_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "monitor.pid"

            service_manager._write_pid(path, 12345)

            self.assertEqual(service_manager._read_pid(path), 12345)


if __name__ == "__main__":
    unittest.main()
