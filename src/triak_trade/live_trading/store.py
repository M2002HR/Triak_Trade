"""JSON-based persistence for live trading sessions and trades."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from triak_trade.live_trading.models import LiveMessageTrace, LiveSession, LiveTrade


class LiveTradingStore:
    """Thread-safe JSON file persistence for live trading state."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _sessions_dir(self) -> Path:
        d = self.root / "sessions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _trades_dir(self, session_id: str) -> Path:
        d = self.root / "trades" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _messages_dir(self, session_id: str) -> Path:
        d = self.root / "messages" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Session CRUD ──────────────────────────────────────────────────────

    def save_session(self, session: LiveSession) -> None:
        path = self._sessions_dir() / f"{session.session_id}.json"
        self._write(path, session.model_dump(mode="json"))

    def load_session(self, session_id: str) -> LiveSession | None:
        path = self._sessions_dir() / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LiveSession.model_validate(data)
        except Exception:
            return None

    def list_sessions(self, limit: int = 20) -> list[LiveSession]:
        sessions_dir = self._sessions_dir()
        files = sorted(sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result: list[LiveSession] = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(LiveSession.model_validate(data))
            except Exception:
                continue
        return result

    def get_active_session(self) -> LiveSession | None:
        active = self.list_active_sessions(limit=1)
        return active[0] if active else None

    def list_active_sessions(self, limit: int = 20) -> list[LiveSession]:
        result: list[LiveSession] = []
        for session in self.list_sessions(limit=limit * 3):
            if session.status in ("running", "starting"):
                result.append(session)
            if len(result) >= limit:
                break
        return result

    # ── Trade CRUD ────────────────────────────────────────────────────────

    def save_trade(self, trade: LiveTrade) -> None:
        path = self._trades_dir(trade.session_id) / f"{trade.trade_id}.json"
        self._write(path, trade.model_dump(mode="json"))

    def load_trade(self, session_id: str, trade_id: str) -> LiveTrade | None:
        path = self._trades_dir(session_id) / f"{trade_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LiveTrade.model_validate(data)
        except Exception:
            return None

    def list_trades(self, session_id: str, limit: int = 200) -> list[LiveTrade]:
        trades_dir = self._trades_dir(session_id)
        files = sorted(trades_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result: list[LiveTrade] = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(LiveTrade.model_validate(data))
            except Exception:
                continue
        return result

    def list_open_trades(self, session_id: str) -> list[LiveTrade]:
        return [t for t in self.list_trades(session_id) if t.is_open]

    def list_closed_trades(self, session_id: str, limit: int = 50) -> list[LiveTrade]:
        closed = [t for t in self.list_trades(session_id) if not t.is_open]
        return sorted(closed, key=lambda t: t.closed_at or t.opened_at, reverse=True)[:limit]

    # ── Message Trace CRUD ───────────────────────────────────────────────

    def save_message_trace(self, session_id: str, trace: LiveMessageTrace) -> None:
        path = self._messages_dir(session_id) / f"{trace.message_id}_{trace.channel_id}.json"
        self._write(path, trace.model_dump(mode="json"))

    def list_message_traces(self, session_id: str, limit: int = 200) -> list[LiveMessageTrace]:
        messages_dir = self._messages_dir(session_id)
        files = sorted(
            messages_dir.glob("*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        result: list[LiveMessageTrace] = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(LiveMessageTrace.model_validate(data))
            except Exception:
                continue
        return result

    # ── Atomic Write ──────────────────────────────────────────────────────

    def _write(self, path: Path, payload: Any) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)
