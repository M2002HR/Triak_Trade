"""JSON-based persistence for live trading sessions and trades."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path
from shutil import rmtree
from typing import Any, TypeVar
from urllib.parse import quote

from sqlalchemy.orm import Session, sessionmaker

from triak_trade.db.repositories import LiveTradingRepository
from triak_trade.live_trading.models import (
    LiveMessageTrace,
    LiveSession,
    LiveSignalSnapshot,
    LiveTrade,
)

T = TypeVar("T")


class LiveTradingStore:
    """Thread-safe persistence for live trading state.

    When ``session_factory`` is provided, DB is the primary state backend.
    Filesystem persistence remains as a local fallback for tests and simple runs.
    """

    def __init__(
        self,
        root_dir: str | Path,
        *,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._session_factory = session_factory

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

    def _signals_dir(self, session_id: str) -> Path:
        d = self.root / "signals" / session_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Session CRUD ──────────────────────────────────────────────────────

    def save_session(self, session: LiveSession) -> None:
        if self._session_factory is not None:
            self._with_db(lambda repo: repo.save_session(session))
            return
        path = self._sessions_dir() / f"{session.session_id}.json"
        self._write(path, session.model_dump(mode="json"))

    def load_session(self, session_id: str) -> LiveSession | None:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.load_session(session_id))
        path = self._sessions_dir() / f"{session_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LiveSession.model_validate(data)
        except Exception:
            return None

    def list_sessions(self, limit: int = 20) -> list[LiveSession]:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.list_sessions(limit=limit))
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
        if self._session_factory is not None:
            self._with_db(lambda repo: repo.save_trade(trade))
            return
        path = self._trades_dir(trade.session_id) / f"{trade.trade_id}.json"
        self._write(path, trade.model_dump(mode="json"))

    def load_trade(self, session_id: str, trade_id: str) -> LiveTrade | None:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.load_trade(session_id, trade_id))
        path = self._trades_dir(session_id) / f"{trade_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LiveTrade.model_validate(data)
        except Exception:
            return None

    def list_trades(self, session_id: str, limit: int = 200) -> list[LiveTrade]:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.list_trades(session_id, limit=limit))
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

    def delete_trade(self, session_id: str, trade_id: str) -> bool:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.delete_trade(session_id, trade_id))
        path = self._trades_dir(session_id) / f"{trade_id}.json"
        return self._delete_file(path)

    # ── Message Trace CRUD ───────────────────────────────────────────────

    def save_message_trace(self, session_id: str, trace: LiveMessageTrace) -> None:
        if self._session_factory is not None:
            self._with_db(lambda repo: repo.save_message_trace(session_id, trace))
            return
        path = self._message_trace_path(
            session_id=session_id,
            message_id=trace.message_id,
            channel_id=trace.channel_id,
        )
        self._write(path, trace.model_dump(mode="json"))

    def list_message_traces(self, session_id: str, limit: int = 200) -> list[LiveMessageTrace]:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.list_message_traces(session_id, limit=limit))
        messages_dir = self._messages_dir(session_id)
        files = sorted(
            messages_dir.rglob("*.json"),
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

    def delete_message_trace(
        self,
        session_id: str,
        message_id: int,
        channel_id: str,
    ) -> bool:
        if self._session_factory is not None:
            return self._with_db(
                lambda repo: repo.delete_message_trace(session_id, message_id, channel_id)
            )
        path = self._message_trace_path(
            session_id=session_id,
            message_id=message_id,
            channel_id=channel_id,
        )
        if self._delete_file(path):
            return True
        legacy_path = self._legacy_message_trace_path(
            session_id=session_id,
            message_id=message_id,
            channel_id=channel_id,
        )
        return self._delete_file(legacy_path)

    # ── Signal CRUD ────────────────────────────────────────────────────────

    def save_signal_snapshot(self, session_id: str, signal: LiveSignalSnapshot) -> None:
        if self._session_factory is not None:
            self._with_db(lambda repo: repo.save_signal_snapshot(session_id, signal))
            return
        path = self._signals_dir(session_id) / f"{signal.signal_id}.json"
        self._write(path, signal.model_dump(mode="json"))

    def load_signal_snapshot(
        self,
        session_id: str,
        signal_id: str,
    ) -> LiveSignalSnapshot | None:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.load_signal_snapshot(session_id, signal_id))
        path = self._signals_dir(session_id) / f"{signal_id}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return LiveSignalSnapshot.model_validate(data)
        except Exception:
            return None

    def list_signal_snapshots(
        self,
        session_id: str,
        limit: int = 200,
    ) -> list[LiveSignalSnapshot]:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.list_signal_snapshots(session_id, limit=limit))
        signals_dir = self._signals_dir(session_id)
        files = sorted(signals_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        result: list[LiveSignalSnapshot] = []
        for path in files[:limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                result.append(LiveSignalSnapshot.model_validate(data))
            except Exception:
                continue
        return result

    def delete_signal_snapshot(self, session_id: str, signal_id: str) -> bool:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.delete_signal_snapshot(session_id, signal_id))
        path = self._signals_dir(session_id) / f"{signal_id}.json"
        return self._delete_file(path)

    # ── Session History Deletion ───────────────────────────────────────────

    def delete_session(self, session_id: str) -> bool:
        if self._session_factory is not None:
            return self._with_db(lambda repo: repo.delete_session(session_id))
        removed = self._delete_file(self._sessions_dir() / f"{session_id}.json")
        for base in ("trades", "messages", "signals"):
            directory = self.root / base / session_id
            if directory.exists():
                with self._lock:
                    rmtree(directory, ignore_errors=True)
                removed = True
        return removed

    # ── Atomic Write ──────────────────────────────────────────────────────

    def _write(self, path: Path, payload: Any) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            tmp.replace(path)

    def _message_trace_path(self, *, session_id: str, message_id: int, channel_id: str) -> Path:
        encoded_channel = quote(channel_id, safe="")
        return self._messages_dir(session_id) / f"{message_id}_{encoded_channel}.json"

    def _legacy_message_trace_path(
        self,
        *,
        session_id: str,
        message_id: int,
        channel_id: str,
    ) -> Path:
        return self._messages_dir(session_id) / f"{message_id}_{channel_id}.json"

    def _delete_file(self, path: Path) -> bool:
        with self._lock:
            if not path.exists():
                return False
            path.unlink()
            return True

    def _with_db(self, fn: Callable[[LiveTradingRepository], T]) -> T:
        assert self._session_factory is not None
        session = self._session_factory()
        try:
            repo = LiveTradingRepository(session)
            result = fn(repo)
            session.commit()
            return result
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
