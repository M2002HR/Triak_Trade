from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.verification.models import (
    VerificationCheckResult,
    VerificationReport,
    VerificationStatus,
)
from triak_trade.verification.report import (
    calculate_totals,
    find_latest_report,
    render_markdown_report,
    render_terminal_summary,
    write_reports,
)


def _report() -> VerificationReport:
    checks = [
        VerificationCheckResult(
            name="config",
            status=VerificationStatus.PASS,
            category="safe",
            summary="ok",
            duration_ms=1,
        )
    ]
    return VerificationReport(
        generated_at=datetime.now(timezone.utc),
        environment_summary={"TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyzABCDE"},
        checks=checks,
        totals=calculate_totals(checks),
        overall_status=VerificationStatus.PASS,
    )


def test_report_render_and_write_redacts(tmp_path) -> None:  # type: ignore[no-untyped-def]
    report = _report()
    terminal = render_terminal_summary(report)
    markdown = render_markdown_report(report)
    assert "abcdefghijklmnopqrstuvwxyz" not in terminal
    assert "abcdefghijklmnopqrstuvwxyz" not in markdown

    written = write_reports(report, str(tmp_path))
    assert written.json_path is not None
    assert written.markdown_path is not None
    assert "abcdefghijklmnopqrstuvwxyz" not in open(written.markdown_path, encoding="utf-8").read()
    assert find_latest_report(str(tmp_path)) is not None
