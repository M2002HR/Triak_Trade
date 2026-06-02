"""Verification report rendering and persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from triak_trade.verification.models import VerificationReport, VerificationStatus
from triak_trade.verification.redaction import redact, redact_text


def calculate_totals(checks: list[Any]) -> dict[str, int]:
    totals = {status.value: 0 for status in VerificationStatus}
    for check in checks:
        totals[check.status.value] += 1
    return totals


def render_terminal_summary(report: VerificationReport) -> str:
    lines = [
        "Triak_Trade System Verification",
        f"Overall: {report.overall_status.value}",
        (
            "Totals: "
            f"PASS={report.totals['PASS']} "
            f"WARN={report.totals['WARN']} "
            f"SKIP={report.totals['SKIP']} "
            f"FAIL={report.totals['FAIL']}"
        ),
        "",
    ]
    for check in report.checks:
        next_action = f" | next: {check.next_action}" if check.next_action else ""
        lines.append(
            f"[{check.status.value}] "
            f"{check.category}/{check.name}: {check.summary}{next_action}"
        )
    if report.markdown_path:
        lines.append(f"Markdown report: {report.markdown_path}")
    if report.json_path:
        lines.append(f"JSON report: {report.json_path}")
    return redact_text("\n".join(lines))


def render_markdown_report(report: VerificationReport) -> str:
    lines = [
        "# Triak_Trade System Verification",
        "",
        f"Generated: `{report.generated_at.isoformat()}`",
        f"Overall status: **{report.overall_status.value}**",
        "",
        "## Environment Summary",
        "",
    ]
    for key, value in redact(report.environment_summary).items():
        lines.append(f"- `{key}`: `{value}`")

    lines.extend(
        [
            "",
            "## Checks",
            "",
            "| Category | Check | Status | Summary | Next action |",
            "| --- | --- | --- | --- | --- |",
        ]
    )
    for check in report.checks:
        lines.append(
            "| "
            f"{check.category} | {check.name} | {check.status.value} | "
            f"{check.summary} | {check.next_action or ''} |"
        )

    problem_checks = [
        check
        for check in report.checks
        if check.status in {VerificationStatus.FAIL, VerificationStatus.WARN}
    ]
    if problem_checks:
        lines.extend(["", "## Failures And Warnings", ""])
        for check in problem_checks:
            lines.append(f"- `{check.name}`: {check.summary}")

    lines.extend(
        [
            "",
            "## Safe Next Steps",
            "",
            "- Run `triak-trade verify-system --mode safe --write-report` for local-safe checks.",
            "- Set `RUN_SYSTEM_REAL_SMOKE_TESTS=1` plus specific guards for real checks.",
            "- Real checks fetch tiny data, print summaries only, and never execute live trades.",
        ]
    )
    return redact_text("\n".join(lines))


def write_reports(report: VerificationReport, report_dir: str) -> VerificationReport:
    target = Path(report_dir)
    target.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = target / f"triak_verification_{stamp}.report.json"
    markdown_path = target / f"triak_verification_{stamp}.report.md"

    report.json_path = str(json_path)
    report.markdown_path = str(markdown_path)
    safe_payload = redact(report.model_dump(mode="json"))
    json_path.write_text(json.dumps(safe_payload, indent=2, sort_keys=True), encoding="utf-8")
    markdown_path.write_text(render_markdown_report(report), encoding="utf-8")
    return report


def find_latest_report(report_dir: str) -> Path | None:
    target = Path(report_dir)
    if not target.exists():
        return None
    reports = sorted(
        target.glob("*.report.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    return reports[0] if reports else None
