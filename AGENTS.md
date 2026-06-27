# AGENTS: Triak_Trade

## Purpose
Triak_Trade is a modular signal intelligence and trading platform.


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
- Every Codex task must include self-verification: run tests, run the implemented interface manually, inspect logs/output, verify success and failure cases, and continue until behavior is correct.
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
