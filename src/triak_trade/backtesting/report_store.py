"""Persistence helpers for real backtest reports."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class StoredBacktestReport:
    json_path: str
    markdown_path: str


class BacktestReportStore:
    def __init__(self, report_dir: str) -> None:
        self.report_dir = Path(report_dir)

    def write(self, payload: dict[str, Any]) -> StoredBacktestReport:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = _channel_slug(str(payload.get("channel", "channel")))
        base_name = f"real_backtest_{slug}_{stamp}"
        json_path = self.report_dir / f"{base_name}.report.json"
        markdown_path = self.report_dir / f"{base_name}.report.md"
        payload = dict(payload)
        payload["report_path"] = str(json_path)
        payload["markdown_report_path"] = str(markdown_path)
        json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        markdown_path.write_text(_render_markdown(payload), encoding="utf-8")
        return StoredBacktestReport(
            json_path=str(json_path),
            markdown_path=str(markdown_path),
        )

    def latest(self) -> Path | None:
        if not self.report_dir.exists():
            return None
        reports = sorted(
            self.report_dir.glob("*.report.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
        return reports[0] if reports else None

    def list_reports(self) -> list[Path]:
        if not self.report_dir.exists():
            return []
        return sorted(
            self.report_dir.glob("*.report.json"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )


def _channel_slug(channel: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in channel).strip("_")[:80] or "channel"


def _render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Real Backtest Report",
        "",
        f"- Channel: `{payload.get('channel')}`",
        f"- Generated: `{payload.get('generated_at')}`",
        f"- Real Telegram: `{payload.get('real_telegram_used')}`",
        f"- Real Market Data: `{payload.get('real_market_data_used')}`",
        f"- AI Used: `{payload.get('ai_used')}`",
        f"- Regex Fallback Used: `{payload.get('regex_fallback_used')}`",
        "",
        "## Summary",
        "",
    ]
    for key in (
        "total_messages",
        "classified_messages",
        "parsed_signals",
        "valid_signals",
        "invalid_signals",
        "ignored_messages",
        "ambiguous_messages",
        "candles_fetched",
        "trades_simulated",
        "trades_filled",
        "wins",
        "losses",
        "win_rate",
        "total_pnl",
        "profit_factor",
        "max_drawdown",
        "conservative_pnl",
        "optimistic_pnl",
        "channel_score",
    ):
        lines.append(f"- `{key}`: `{payload.get(key)}`")

    warnings = payload.get("warnings") or []
    errors = payload.get("errors") or []
    skipped = payload.get("skipped_reasons") or []
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {item}" for item in warnings)
    if errors:
        lines.extend(["", "## Errors", ""])
        lines.extend(f"- {item}" for item in errors)
    if skipped:
        lines.extend(["", "## Skipped Reasons", ""])
        lines.extend(f"- {item}" for item in skipped)
    return "\n".join(lines)
