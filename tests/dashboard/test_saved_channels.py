from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from triak_trade.config.settings import Settings
from triak_trade.dashboard.saved_channels import SavedChannelStore
from triak_trade.dashboard.schemas import SavedChannelEntry, SavedChannelsState


def _entry(channel: str) -> SavedChannelEntry:
    resolved = f"https://t.me/{channel.removeprefix('@')}"
    return SavedChannelEntry(
        channel_input=f"@{channel.removeprefix('@')}",
        channel_resolved=resolved,
        label=f"@{channel.removeprefix('@')}",
        created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )


def test_shared_store_merges_existing_backtest_and_live_channel_files(
    tmp_path: Path,
) -> None:
    dashboard_dir = tmp_path / "dashboard"
    live_dir = tmp_path / "live"
    shared_path = dashboard_dir / "state" / "saved_channels.json"
    legacy_live_path = live_dir / "state" / "saved_channels.json"
    shared_path.parent.mkdir(parents=True)
    legacy_live_path.parent.mkdir(parents=True)
    shared_path.write_text(
        SavedChannelsState(channels=[_entry("back_saved")]).model_dump_json(),
        encoding="utf-8",
    )
    legacy_live_path.write_text(
        SavedChannelsState(channels=[_entry("live_saved")]).model_dump_json(),
        encoding="utf-8",
    )
    settings = Settings(
        _env_file=None,
        DASHBOARD_RUNTIME_DIR=str(dashboard_dir),
        LIVE_TRADING_RUNTIME_DIR=str(live_dir),
        REAL_BACKTEST_DEFAULT_CHANNEL="@back_default",
        LIVE_TRADING_DEFAULT_CHANNELS=["@live_default"],
    )

    store = SavedChannelStore(settings)
    state = store.get()

    assert [item.channel_resolved for item in state.channels] == [
        "https://t.me/back_saved",
        "https://t.me/live_saved",
        "https://t.me/back_default",
        "https://t.me/live_default",
    ]
    assert store.migration_marker.exists()

    store.remove("@live_saved")

    assert "https://t.me/live_saved" not in {
        item.channel_resolved for item in SavedChannelStore(settings).get().channels
    }


def test_shared_store_deduplicates_equivalent_channel_references(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        DASHBOARD_RUNTIME_DIR=str(tmp_path / "dashboard"),
        LIVE_TRADING_RUNTIME_DIR=str(tmp_path / "live"),
        REAL_BACKTEST_DEFAULT_CHANNEL="@same_channel",
        LIVE_TRADING_DEFAULT_CHANNELS=["https://t.me/SAME_CHANNEL"],
    )

    state = SavedChannelStore(settings).get()

    assert [
        item.channel_resolved
        for item in state.channels
        if item.channel_resolved == "https://t.me/same_channel"
    ] == ["https://t.me/same_channel"]
