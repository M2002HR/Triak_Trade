#!/usr/bin/env bash
# run_ghahr_backtest.sh
# Validates all code fixes then runs a 7-day real backtest on @ghahr
# Usage:  bash scripts/run_ghahr_backtest.sh
set -euo pipefail

CHANNEL="https://t.me/ghahr"
HOURS=168          # 7 days
INTERVAL="1m"
MAX_MESSAGES=5000

cd "$(dirname "$0")/.."

echo "=== 1/4  Running test suite ==="
python -m pytest tests/ -q --tb=short
echo ""

echo "=== 2/4  Ruff lint ==="
python -m ruff check .
echo ""

echo "=== 3/4  Mypy type check ==="
python -m mypy src --ignore-missing-imports 2>&1 | tail -20
echo ""

echo "=== 4/4  Real backtest — @ghahr (7 days, interval=${INTERVAL}) ==="
echo "    Channel : ${CHANNEL}"
echo "    Hours   : ${HOURS}"
echo "    Messages: ${MAX_MESSAGES}"
echo ""

# The real-backtest-run command needs these integration guards to be set.
# They are read from .env.local — verify they are present:
if ! grep -q "RUN_BACKTEST_INTEGRATION_TESTS=1" .env.local 2>/dev/null; then
    echo "ERROR: RUN_BACKTEST_INTEGRATION_TESTS=1 not found in .env.local"
    echo "Add the following to .env.local and re-run:"
    echo "  RUN_BACKTEST_INTEGRATION_TESTS=1"
    echo "  RUN_TELEGRAM_INTEGRATION_TESTS=1"
    echo "  RUN_BINANCE_PUBLIC_MARKETDATA_INTEGRATION_TESTS=1"
    exit 1
fi

triak-trade real-backtest-run \
    --channel "${CHANNEL}" \
    --hours "${HOURS}" \
    --interval "${INTERVAL}" \
    --max-messages "${MAX_MESSAGES}" \
    --no-ai \
    --no-send-log-channel

echo ""
echo "=== Done.  Latest report: ==="
triak-trade backtest-show-latest 2>/dev/null || echo "(no stored report found — check logs above)"
