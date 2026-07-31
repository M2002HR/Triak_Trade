"""Shared saved-channel storage for dashboard trading workflows."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from triak_trade.config.settings import Settings
from triak_trade.dashboard.backtest_runtime import normalize_channel_reference
from triak_trade.dashboard.schemas import SavedChannelEntry, SavedChannelsState


class SavedChannelStore:
    """Persist one channel library shared by Backtest and Live Trade."""

    _lock = threading.RLock()
    _migration_version = 1

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.state_dir = Path(settings.DASHBOARD_RUNTIME_DIR) / "state"
        self.path = self.state_dir / "saved_channels.json"
        self.migration_marker = self.state_dir / "saved_channels_shared_migration.json"
        self.legacy_live_path = (
            Path(settings.LIVE_TRADING_RUNTIME_DIR) / "state" / "saved_channels.json"
        )

    def get(self) -> SavedChannelsState:
        with self._lock:
            self._migrate_once()
            if self.path.exists():
                return self._read(self.path)
            state = SavedChannelsState(channels=self._default_entries())
            self._write(self.path, state.model_dump(mode="json"))
            return state

    def add(self, channel_input: str) -> SavedChannelsState:
        normalized_input = channel_input.strip()
        if not normalized_input:
            raise ValueError("channel is required")
        resolved = normalize_channel_reference(normalized_input)
        identity = self._identity(resolved)
        with self._lock:
            state = self.get()
            channels = [
                item
                for item in state.channels
                if self._identity(item.channel_resolved) != identity
            ]
            channels.insert(
                0,
                SavedChannelEntry(
                    channel_input=normalized_input,
                    channel_resolved=resolved,
                    label=self._channel_label(resolved),
                    created_at=self._utc_now(),
                ),
            )
            updated = SavedChannelsState(channels=channels[:50])
            self._write(self.path, updated.model_dump(mode="json"))
            return updated

    def remove(self, channel_reference: str) -> SavedChannelsState:
        normalized_reference = channel_reference.strip()
        if not normalized_reference:
            raise ValueError("channel is required")
        resolved = normalize_channel_reference(normalized_reference)
        identity = self._identity(resolved)
        with self._lock:
            state = self.get()
            updated = SavedChannelsState(
                channels=[
                    item
                    for item in state.channels
                    if self._identity(item.channel_resolved) != identity
                ]
            )
            self._write(self.path, updated.model_dump(mode="json"))
            return updated

    def _migrate_once(self) -> None:
        if self.migration_marker.exists():
            return
        entries: list[SavedChannelEntry] = []
        if self.path.exists():
            entries.extend(self._read(self.path).channels)
        if self.legacy_live_path != self.path and self.legacy_live_path.exists():
            entries.extend(self._read(self.legacy_live_path).channels)
        entries.extend(self._default_entries())

        merged: list[SavedChannelEntry] = []
        seen: set[str] = set()
        for entry in entries:
            resolved = normalize_channel_reference(entry.channel_resolved)
            identity = self._identity(resolved)
            if identity in seen:
                continue
            seen.add(identity)
            merged.append(
                entry.model_copy(
                    update={
                        "channel_resolved": resolved,
                        "label": self._channel_label(resolved),
                    }
                )
            )
        state = SavedChannelsState(channels=merged[:50])
        self._write(self.path, state.model_dump(mode="json"))
        self._write(
            self.migration_marker,
            {
                "version": self._migration_version,
                "migrated_at": self._utc_now().isoformat(),
                "shared_path": str(self.path),
                "legacy_live_path": str(self.legacy_live_path),
                "channel_count": len(state.channels),
            },
        )

    def _default_entries(self) -> list[SavedChannelEntry]:
        configured = [
            self.settings.REAL_BACKTEST_DEFAULT_CHANNEL,
            *self.settings.LIVE_TRADING_DEFAULT_CHANNELS,
        ]
        entries: list[SavedChannelEntry] = []
        seen: set[str] = set()
        for channel_input in configured:
            normalized_input = str(channel_input).strip()
            if not normalized_input:
                continue
            resolved = normalize_channel_reference(normalized_input)
            identity = self._identity(resolved)
            if identity in seen:
                continue
            seen.add(identity)
            entries.append(
                SavedChannelEntry(
                    channel_input=normalized_input,
                    channel_resolved=resolved,
                    label=self._channel_label(resolved),
                    created_at=self._utc_now(),
                )
            )
        return entries

    @staticmethod
    def _read(path: Path) -> SavedChannelsState:
        return SavedChannelsState.model_validate_json(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _channel_label(channel_reference: str) -> str:
        if channel_reference.lstrip("-").isdigit():
            return channel_reference
        if channel_reference.startswith("https://t.me/"):
            return f"@{channel_reference.rsplit('/', 1)[-1]}"
        if channel_reference.startswith("@"):
            return channel_reference
        return f"@{channel_reference}"

    @staticmethod
    def _identity(channel_reference: str) -> str:
        if channel_reference.startswith("https://t.me/") or channel_reference.startswith(
            "@"
        ):
            return channel_reference.casefold()
        return channel_reference

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)
