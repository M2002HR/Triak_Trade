from __future__ import annotations

from datetime import datetime, timezone

from triak_trade.verification.models import (
    VerificationCheckResult,
    VerificationReport,
    VerificationStatus,
)
from triak_trade.verification.report import calculate_totals


def test_verification_status_and_totals() -> None:
    checks = [
        VerificationCheckResult(
            name="a",
            status=VerificationStatus.PASS,
            category="safe",
            summary="ok",
            duration_ms=1,
        ),
        VerificationCheckResult(
            name="b",
            status=VerificationStatus.SKIP,
            category="real",
            summary="guard",
            duration_ms=1,
        ),
    ]
    report = VerificationReport(
        generated_at=datetime.now(timezone.utc),
        environment_summary={},
        checks=checks,
        totals=calculate_totals(checks),
        overall_status=VerificationStatus.PASS,
    )
    assert report.totals["PASS"] == 1
    assert report.totals["SKIP"] == 1
