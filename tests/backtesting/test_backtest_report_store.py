from __future__ import annotations

from pathlib import Path

from triak_trade.backtesting.report_store import BacktestReportStore


def test_report_store_keeps_rapid_backtest_runs_as_distinct_artifacts(tmp_path: Path) -> None:
    store = BacktestReportStore(str(tmp_path))

    first = store.write({"channel": "https://t.me/sample", "generated_at": "first"})
    second = store.write({"channel": "https://t.me/sample", "generated_at": "second"})

    assert first.json_path != second.json_path
    assert len(store.list_reports()) == 2
    assert list(tmp_path.rglob("*.tmp")) == []


def test_report_store_path_hints_update_the_same_run_artifact(tmp_path: Path) -> None:
    store = BacktestReportStore(str(tmp_path))
    first = store.write({"channel": "https://t.me/sample", "generated_at": "first"})

    updated = store.write(
        {
            "channel": "https://t.me/sample",
            "generated_at": "updated",
            "report_path": first.json_path,
            "markdown_report_path": first.markdown_path,
        }
    )

    assert updated == first
    assert len(store.list_reports()) == 1
