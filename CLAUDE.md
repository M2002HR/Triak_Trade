# CLAUDE: Triak_Trade

> **This file and `AGENTS.md` MUST stay synchronized.**
> Both files describe the same project rules for any AI agent working on this repository
> (Claude Code, Codex, or any other agent). Whenever a rule is added, changed, or removed
> in one file, the exact same change MUST be applied to the other file in the same task.
> A rule that exists in only one of the two files is a bug. Before finishing any task that
> touches project rules, diff the "Rules" sections of `CLAUDE.md` and `AGENTS.md` and
> confirm they match.

## Purpose

Triak_Trade is a modular signal intelligence and trading platform.

## Reporting To Telegram (Standing Instruction)

Whenever the user asks for a report to be sent to Telegram ("گزارش رو توی تلگرام برام بفرست"
or any equivalent request):

- Send it to the Telegram ID **`we_are_waiting_for_him`**.
- Send it using the **existing Telethon user session**, i.e. the credentials already present
  in the root `.env.local`: `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`,
  `TELEGRAM_STRING_SESSION`, and the configured `TELEGRAM_PROXY_*` settings.
- Do **not** use the bot API / `@triak_logs` log channel for these user-requested reports.
  The `@triak_logs` channel remains reserved for automated processing-audit reporting.
- Never print or commit the session string, API hash, or any other secret while doing this.
- Reports are only sent when the user asks. Do not send unrequested messages.

## Rules

- Always work module-by-module.
- Always write tests for each module.
- Always run tests before finishing.
- Always run `ruff check .`, `mypy src`, and `pytest`.
- Integration tests must be guarded by explicit environment flags.
- Never print secrets.
- Never commit secrets.
- All financial values must use `Decimal`, never `float`.
- External services must be behind interfaces/adapters.
- Logs should be structured and useful for both humans and AI debugging.
- Every module should be independently testable.
- Integrate Ajil gateway later as a Git submodule or local external dependency.
- Runtime configuration single source of truth is root `Triak_Trade/.env.local`.
- Do not create or use a separate `.env` inside `Ajil_Unified_AI_Gateway`.
- Every agent task must include self-verification: run tests, run the implemented interface manually, inspect logs/output, verify success and failure cases, and continue until behavior is correct.
- Final production parsing/classification must be AI-driven and agentic via Ajil Unified AI Gateway.
- Regex parsing is only baseline/fallback/safety, not the final decision engine.
- Keep core logic generalized across channels, including noisy/ambiguous/update/cancel/report/ad content.
- AI gateway integration tests must be explicitly guarded; default unit tests use fakes/mocks and no real AI calls.
- Telethon integration must remain behind interfaces and use fakes in unit tests.
- Never commit Telegram session artifacts (`.sessions/`, `*.session*`).
- Real Telegram integration tests must be explicitly guarded via environment flags.
- `https://t.me/Tofan_Trade` is a future/guarded real-world test target only; never hard-code channel-specific rules.
- Market data providers must stay behind interfaces; Toobit public klines is first provider.
- Mandatory real integration verification policy:
- All real credentials must come only from root `Triak_Trade/.env.local`.
- Before finishing any task: run `ruff check .`, `mypy src`, `pytest`, run module dry-run CLI, and if a guard is enabled run the smallest real integration check, then manually inspect outputs/logs for safety and correctness.
- AI gateway real check guard: `RUN_AI_GATEWAY_INTEGRATION_TESTS=1` (safe classification samples, no key exposure, ads/results must not be new signals).
- Backtesting engine is simulation-only and must never execute real trades.
- Backtesting must use classifier interfaces/protocols (AI-ready), not regex internals directly.
- Real backtest pipeline may use Telethon history, Ajil AI when available, and Toobit public klines only; it must never use private trading endpoints.
- Real backtest reports must be stored under `runtime/reports/backtests` and remain non-secret.
- If AI is unavailable during real backtest, report it explicitly and use regex fallback only when configured; never pretend AI was used.
- Processing audit events must capture safe per-message operational visibility without secrets.
- Telegram processing log channel is `@triak_logs`; all log-channel reports must be in English.
- Real log-channel sending requires `TELEGRAM_LOG_CHANNEL_ENABLED=true`, `PROCESSING_AUDIT_SEND_TO_LOG_CHANNEL=true`, and `RUN_TELEGRAM_LOG_CHANNEL_INTEGRATION_TESTS=1`.
- Do not scatter direct log-channel sends through core logic; use observability event/reporting services.
- Dashboard auth tokens and session secrets must live only in root `.env.local` and must never be printed.
- Dashboard Auto Mode and Kill Switch are runtime state only until future Risk Engine/Demo Execution modules exist.
- User-requested reports go to the Telegram ID `we_are_waiting_for_him` through the existing Telethon user session, never through the bot API or `@triak_logs`.
- `CLAUDE.md` and `AGENTS.md` must always stay synchronized; any rule change must be applied to both files in the same task.
- After completing every agent task, always rebuild the Docker images with `docker compose build`, bring the stack up with `docker compose up -d`, and verify `docker compose ps` plus relevant health/log output before the final response. Never skip this final Docker rebuild-and-start verification; if Docker is genuinely unavailable, report the blocker explicitly.

## Live Trading Quick Reference

Useful context when working on the live/demo execution path:

- Engine: `src/triak_trade/live_trading/engine.py`
- Sizing: `src/triak_trade/live_trading/position_manager.py`
- Multi-session coordination: `src/triak_trade/live_trading/account_coordinator.py`
- Exchange execution: `src/triak_trade/exchange/toobit/futures.py`
- Dashboard session control: `src/triak_trade/dashboard/live_runtime.py`

`risk_per_trade_pct` (default `120`) is a **risk factor**, not a direct percentage of
balance. Allocation is derived in `_allocation_pct_for_signal()` as
`risk_per_trade_pct / leverage`, then clamped between
`LIVE_TRADING_MIN_ALLOCATION_PCT` and `LIVE_TRADING_MAX_ALLOCATION_PCT`.
`LIVE_TRADING_HARD_MAX_RISK_FACTOR_PCT` (also `120`) is the hard ceiling enforced in
`live_runtime.start_session()`.
