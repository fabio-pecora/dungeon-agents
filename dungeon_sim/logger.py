from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from .models import AgentStepEvent, RunSummary


class RunLogger:
    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_dir / "events.jsonl"
        self.summary_path = self.run_dir / "summary.json"

    def write_event(self, event: AgentStepEvent) -> None:
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json())
            f.write("\n")

    def write_summary(self, summary: RunSummary) -> None:
        self.summary_path.write_text(summary.model_dump_json(indent=2), encoding="utf-8")

    def read_events(self) -> list[dict]:
        if not self.events_path.exists():
            return []
        rows = []
        with self.events_path.open("r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    rows.append(json.loads(line))
        return rows
