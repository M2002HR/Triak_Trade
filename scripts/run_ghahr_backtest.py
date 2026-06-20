#!/usr/bin/env python
"""
Run tests + 7-day real backtest on @ghahr.

Usage:
    python scripts/run_ghahr_backtest.py

Requires in .env.local:
    RUN_BACKTEST_INTEGRATION_TESTS=1
    RUN_TELEGRAM_INTEGRATION_TESTS=1
    RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def run(cmd: list[str], *, label: str) -> bool:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=ROOT)
    ok = result.returncode == 0
    status = "PASSED ✓" if ok else "FAILED ✗"
    print(f"\n  → {status}")
    return ok


def main() -> None:
    failures: list[str] = []

    # ── 1. Tests ────────────────────────────────────────────────────────────
    ok = run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--tb=short"],
        label="1/4  pytest",
    )
    if not ok:
        failures.append("pytest")

    # ── 2. Ruff lint ────────────────────────────────────────────────────────
    ok = run(
        [sys.executable, "-m", "ruff", "check", "."],
        label="2/4  ruff check",
    )
    if not ok:
        failures.append("ruff")

    # ── 3. Mypy ─────────────────────────────────────────────────────────────
    ok = run(
        [sys.executable, "-m", "mypy", "src", "--ignore-missing-imports"],
        label="3/4  mypy src",
    )
    if not ok:
        failures.append("mypy")

    if failures:
        print(f"\n✗ Checks failed: {failures}")
        print("Fix the issues above before running the backtest.")
        sys.exit(1)

    # ── 4. Real backtest ────────────────────────────────────────────────────
    channel = "https://t.me/ghahr"
    print(f"\n{'='*60}")
    print(f"  4/4  Real backtest — {channel} (7 days)")
    print(f"{'='*60}")
    result = subprocess.run(
        [
            "triak-trade",
            "real-backtest-run",
            "--channel", channel,
            "--hours", "168",
            "--interval", "1m",
            "--max-messages", "5000",
            "--no-ai",
            "--no-send-log-channel",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.stdout:
        try:
            data = json.loads(result.stdout)
            print(json.dumps(data, indent=2, ensure_ascii=False))
        except Exception:
            print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[-3000:])
    if result.returncode != 0:
        print("\n✗ Backtest failed (see above).")
        sys.exit(result.returncode)
    print("\n✓ Backtest complete.")


if __name__ == "__main__":
    main()
