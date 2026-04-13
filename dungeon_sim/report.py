from __future__ import annotations

from pathlib import Path

from .models import RunSummary


def write_report(run_dir: Path, summary: RunSummary, events: list[dict]) -> None:
    report_path = run_dir / "report.md"
    lines: list[str] = []
    lines.append(f"# Dungeon Run Report: {summary.run_id}")
    lines.append("")
    lines.append("## 1. Outcome")
    lines.append(f"- Success: **{summary.success}**")
    lines.append(f"- Outcome: {summary.outcome}")
    lines.append(f"- Turns executed: {summary.turns_executed}/{summary.turn_limit}")
    lines.append(f"- Model: {summary.model}")
    lines.append("")
    lines.append("## 2. What happened")
    if not events:
        lines.append("- No events were recorded.")
    else:
        for event in events[:12]:
            agent = event["agent_id"]
            turn = event["turn"]
            intent = event["decision"]["intent_summary"]
            result = event["action_execution"]["result"]
            lines.append(f"- Turn {turn}, Agent {agent}: {intent}. Result: {result}")
    lines.append("")
    lines.append("## 3. Why it happened")
    if summary.incidents:
        for incident in summary.incidents:
            lines.append(f"- {incident}")
    else:
        lines.append("- No major incidents were detected.")
    if summary.communication_findings:
        for finding in summary.communication_findings:
            lines.append(f"- {finding}")
    lines.append("")
    lines.append("## 4. What should change next")
    if summary.recommendations:
        for rec in summary.recommendations:
            lines.append(f"- {rec}")
    else:
        lines.append("- Keep the same setup.")
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
