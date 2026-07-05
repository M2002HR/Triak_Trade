"""Verification runner."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from triak_trade.config.settings import Settings
from triak_trade.core.logging import log_event
from triak_trade.verification.checks import real_checks, safe_checks
from triak_trade.verification.models import (
    VerificationCheckResult,
    VerificationReport,
    VerificationStatus,
)
from triak_trade.verification.redaction import redact
from triak_trade.verification.report import calculate_totals, write_reports

VerificationMode = Literal["safe", "real", "all"]

log = logging.getLogger(__name__)


class VerificationRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(
        self,
        *,
        mode: VerificationMode = "safe",
        write_report: bool = False,
    ) -> VerificationReport:
        log_event(
            log,
            logging.INFO,
            "verification.run_started",
            mode=mode,
            write_report=write_report,
        )
        checks = []
        if mode in {"safe", "all"}:
            checks.extend(safe_checks())
        if mode in {"real", "all"}:
            checks.extend(real_checks())
        log_event(
            log,
            logging.INFO,
            "verification.checks_selected",
            mode=mode,
            check_count=len(checks),
        )

        results: list[VerificationCheckResult] = []
        for check in checks:
            try:
                result = check(self.settings)
                results.append(result)
                log_event(
                    log,
                    logging.DEBUG,
                    "verification.check_completed",
                    check_name=result.name,
                    category=result.category,
                    status=result.status.value,
                    duration_ms=result.duration_ms,
                )
            except Exception as exc:
                results.append(
                    VerificationCheckResult(
                        name=getattr(check, "__name__", "unknown"),
                        status=VerificationStatus.FAIL,
                        category="safe" if mode == "safe" else "real",
                        summary="verification check raised",
                        details={},
                        duration_ms=0,
                        error_type=type(exc).__name__,
                    )
                )
                log_event(
                    log,
                    logging.WARNING,
                    "verification.check_failed_with_exception",
                    check_name=getattr(check, "__name__", "unknown"),
                    category="safe" if mode == "safe" else "real",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

        report = VerificationReport(
            generated_at=datetime.now(timezone.utc),
            environment_summary=redact(self._environment_summary()),
            checks=[
                VerificationCheckResult.model_validate(redact(item.model_dump()))
                for item in results
            ],
            totals=calculate_totals(results),
            overall_status=self._overall_status(results, mode),
        )
        if write_report:
            report = write_reports(report, self.settings.VERIFICATION_REPORT_DIR)
        log_event(
            log,
            logging.INFO,
            "verification.run_completed",
            mode=mode,
            overall_status=report.overall_status.value,
            total_checks=len(report.checks),
            passed=report.totals.get("PASS", 0),
            warned=report.totals.get("WARN", 0),
            failed=report.totals.get("FAIL", 0),
            skipped=report.totals.get("SKIP", 0),
            report_path=report.json_path,
        )
        return report

    def _environment_summary(self) -> dict[str, object]:
        return {
            "app_env": self.settings.APP_ENV,
            "execution_mode": self.settings.EXECUTION_MODE,
            "ai_gateway_enabled": self.settings.AI_GATEWAY_ENABLED,
            "telegram_credentials_present": (
                self.settings.TELEGRAM_API_ID > 0
                and self.settings.TELEGRAM_API_HASH.get_secret_value() != "replace_me"
            ),
            "telegram_bot_token_present": (
                self.settings.TELEGRAM_BOT_TOKEN.get_secret_value() != "replace_me"
            ),
            "toobit_key_present": self.settings.TOOBIT_API_KEY.get_secret_value() != "replace_me",
        }

    @staticmethod
    def _overall_status(
        results: list[VerificationCheckResult],
        mode: VerificationMode,
    ) -> VerificationStatus:
        safe_failed = any(
            item.category == "safe" and item.status is VerificationStatus.FAIL for item in results
        )
        if safe_failed:
            return VerificationStatus.FAIL
        any_failed = any(item.status is VerificationStatus.FAIL for item in results)
        if any_failed:
            return VerificationStatus.WARN
        if mode in {"real", "all"} and any(
            item.status is VerificationStatus.SKIP for item in results
        ):
            return VerificationStatus.WARN
        if any(item.status is VerificationStatus.WARN for item in results):
            return VerificationStatus.WARN
        return VerificationStatus.PASS
