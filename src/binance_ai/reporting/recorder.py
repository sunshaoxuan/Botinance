from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from binance_ai.models import CycleReport


class ReportRecorder:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.cycle_log_path = self.output_dir / "cycle_reports.jsonl"
        self.latest_report_path = self.output_dir / "latest_report.json"

    def record_cycle(self, report: CycleReport) -> None:
        payload = json.dumps(asdict(report), ensure_ascii=True)
        with self.cycle_log_path.open("a", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
        self.latest_report_path.write_text(
            json.dumps(asdict(report), ensure_ascii=True, indent=2),
            encoding="utf-8",
        )
