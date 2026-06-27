from __future__ import annotations

from triak_trade.db import models  # noqa: F401
from triak_trade.db.base import Base


def test_all_required_tables_present() -> None:
    names = set(Base.metadata.tables.keys())
    expected = {
        "telegram_messages",
        "normalized_messages",
        "signals",
        "candles",
        "channel_metrics",
        "audit_logs",
        "llm_call_logs",
        "live_sessions",
        "live_trades",
        "live_message_traces",
        "live_signal_snapshots",
    }
    assert expected.issubset(names)
