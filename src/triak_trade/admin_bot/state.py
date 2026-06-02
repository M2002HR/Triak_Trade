"""File-backed runtime state for the Telegram admin bot."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from triak_trade.config.settings import Settings
from triak_trade.verification.redaction import redact, redact_text


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AdminBotRuntimeState(BaseModel):
    """Non-secret runtime status persisted for supervision and CLI inspection."""

    running: bool = False
    pid: int | None = None
    started_at: datetime | None = None
    last_heartbeat_at: datetime | None = None
    restart_count: int = 0
    last_update_id: int | None = None
    last_admin_username: str | None = None
    last_error_type: str | None = None
    last_error_message_redacted: str | None = None
    handled_updates_count: int = 0
    mode: str = "fake"
    watch: bool = False
    notes: list[str] = Field(default_factory=list)


class AdminBotStateStore:
    """Persist status, pid, offset, and redacted log lines under runtime/admin_bot."""

    def __init__(self, settings: Settings) -> None:
        self.runtime_dir = Path(settings.ADMIN_BOT_RUNTIME_DIR)
        self.pid_file = Path(settings.ADMIN_BOT_PID_FILE)
        self.status_file = Path(settings.ADMIN_BOT_STATUS_FILE)
        self.log_file = Path(settings.ADMIN_BOT_LOG_FILE)
        self.offset_file = Path(settings.ADMIN_BOT_OFFSET_FILE)

    def ensure_runtime_dir(self) -> None:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)

    def read_status(self) -> AdminBotRuntimeState:
        if not self.status_file.exists():
            return AdminBotRuntimeState()
        try:
            data = json.loads(self.status_file.read_text(encoding="utf-8"))
            return AdminBotRuntimeState.model_validate(data)
        except Exception as exc:
            return AdminBotRuntimeState(
                running=False,
                last_error_type=type(exc).__name__,
                last_error_message_redacted="status file parse failed",
            )

    def write_status(self, state: AdminBotRuntimeState) -> None:
        self.ensure_runtime_dir()
        safe_data = redact(state.model_dump(mode="json"))
        self._atomic_write_json(self.status_file, safe_data)

    def read_offset(self) -> int | None:
        if not self.offset_file.exists():
            return None
        try:
            data = json.loads(self.offset_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        value = data.get("offset") if isinstance(data, dict) else None
        return value if isinstance(value, int) else None

    def write_offset(self, offset: int) -> None:
        self.ensure_runtime_dir()
        self._atomic_write_json(self.offset_file, {"offset": offset})

    def write_pid(self, pid: int) -> None:
        self.ensure_runtime_dir()
        self.pid_file.write_text(str(pid), encoding="utf-8")

    def read_pid(self) -> int | None:
        if not self.pid_file.exists():
            return None
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            return None

    def remove_pid(self) -> None:
        if self.pid_file.exists():
            self.pid_file.unlink()

    def append_log(self, event: str, payload: dict[str, Any] | None = None) -> None:
        self.ensure_runtime_dir()
        line = {
            "timestamp": utc_now().isoformat(),
            "event": event,
            "payload": redact(payload or {}),
        }
        text = redact_text(json.dumps(line, sort_keys=True, ensure_ascii=False))
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(text + "\n")

    def tail_logs(self, lines: int) -> list[str]:
        if not self.log_file.exists():
            return []
        content = self.log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        return [redact_text(line) for line in content[-lines:]]

    def pid_is_running(self, pid: int | None = None) -> bool:
        target = pid if pid is not None else self.read_pid()
        if target is None:
            return False
        try:
            os.kill(target, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _atomic_write_json(self, path: Path, payload: Any) -> None:
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)
