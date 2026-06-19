"""Helpers for updating root runtime configuration safely."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


class RootEnvConfigEditor:
    """Update selected `.env.local` keys while preserving unrelated lines."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def update_values(self, updates: Mapping[str, str]) -> None:
        normalized_updates = {
            key: self._serialize_value(value)
            for key, value in updates.items()
        }
        existing_lines = (
            self.path.read_text(encoding="utf-8").splitlines()
            if self.path.exists()
            else []
        )
        rendered_lines: list[str] = []
        seen: set[str] = set()

        for line in existing_lines:
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#") or "=" not in line:
                rendered_lines.append(line)
                continue

            key, _sep, _rest = line.partition("=")
            env_key = key.strip()
            if env_key in normalized_updates:
                rendered_lines.append(f"{env_key}={normalized_updates[env_key]}")
                seen.add(env_key)
                continue
            rendered_lines.append(line)

        if normalized_updates and rendered_lines and rendered_lines[-1].strip():
            rendered_lines.append("")

        for key, value in normalized_updates.items():
            if key in seen:
                continue
            rendered_lines.append(f"{key}={value}")

        payload = "\n".join(rendered_lines).rstrip() + "\n"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(payload, encoding="utf-8")

    @staticmethod
    def _serialize_value(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
