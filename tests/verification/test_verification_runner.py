from __future__ import annotations

import logging

import triak_trade.verification.runner as runner_module
from triak_trade.config.settings import Settings
from triak_trade.verification.models import VerificationCheckResult, VerificationStatus
from triak_trade.verification.runner import VerificationRunner


def _result(
    name: str,
    status: VerificationStatus,
    category: str = "safe",
) -> VerificationCheckResult:
    return VerificationCheckResult(
        name=name,
        status=status,
        category=category,
        summary=name,
        duration_ms=1,
    )


def test_runner_safe_pass_and_safe_fail(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        runner_module,
        "safe_checks",
        lambda: [lambda settings: _result("ok", VerificationStatus.PASS)],
    )
    report = VerificationRunner(Settings()).run(mode="safe")
    assert report.overall_status is VerificationStatus.PASS

    monkeypatch.setattr(
        runner_module,
        "safe_checks",
        lambda: [lambda settings: _result("bad", VerificationStatus.FAIL)],
    )
    report = VerificationRunner(Settings()).run(mode="safe")
    assert report.overall_status is VerificationStatus.FAIL


def test_runner_real_skips_warn(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        runner_module,
        "real_checks",
        lambda: [lambda settings: _result("skip", VerificationStatus.SKIP, "real")],
    )
    report = VerificationRunner(Settings()).run(mode="real")
    assert report.overall_status is VerificationStatus.WARN


def test_runner_emits_structured_logs(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        runner_module,
        "safe_checks",
        lambda: [lambda settings: _result("ok", VerificationStatus.PASS)],
    )
    with caplog.at_level(logging.INFO):
        VerificationRunner(Settings()).run(mode="safe")

    events = [rec.msg for rec in caplog.records]
    assert "verification.run_started" in events
    assert "verification.checks_selected" in events
    assert "verification.run_completed" in events
    completed = next(rec for rec in caplog.records if rec.msg == "verification.run_completed")
    assert completed.overall_status == "PASS"
