# AGENTS: Triak_Trade

## Purpose
Triak_Trade is a modular signal intelligence and demo-trading platform with strict safety and testability constraints.

## Rules
- Always work module-by-module.
- Always write tests for each module.
- Always run tests before finishing.
- Always run `ruff check .`, `mypy src`, and `pytest`.
- Never call real Telegram, Toobit, Gemini, Groq, MySQL, or Redis in unit tests.
- Integration tests must be guarded by explicit environment flags.
- Never place live orders.
- Live trading is blocked for now.
- Never print secrets.
- Never commit secrets.
- All financial values must use `Decimal`, never `float`.
- External services must be behind interfaces/adapters.
- Logs should be structured and useful for both humans and AI debugging.
- Every module should be independently testable.
- When implementing Telegram/Telethon later, inspect/reference local sibling `../Broccoli_Bot` for setup/session patterns. Never copy secrets.
- When implementing AI engine integration later, use local sibling `../Ajil_Unified_AI_Gateway`.
- Integrate Ajil gateway later as a Git submodule or local external dependency.
- Runtime configuration single source of truth is root `Triak_Trade/.env.local`.
- Do not create or use a separate `.env` inside `Ajil_Unified_AI_Gateway`.
- Every Codex task must include self-verification: run tests, run the implemented interface manually, inspect logs/output, verify success and failure cases, and continue until behavior is correct.
- Final production parsing/classification must be AI-driven and agentic via Ajil Unified AI Gateway.
- Regex parsing is only baseline/fallback/safety, not the final decision engine.
- First real-world Telegram channel for future live-like evaluation is `https://t.me/Tofan_Trade`.
- Use Tofan_Trade only when Telegram + AI integration is implemented; do not hard-code channel-specific logic.
- Keep core logic generalized across channels, including noisy/ambiguous/update/cancel/report/ad content.
- AI gateway integration tests must be explicitly guarded; default unit tests use fakes/mocks and no real AI calls.
- Telethon integration must remain behind interfaces and use fakes in unit tests.
- Never commit Telegram session artifacts (`.sessions/`, `*.session*`).
- Real Telegram integration tests must be explicitly guarded via environment flags.
- `https://t.me/Tofan_Trade` is a future/guarded real-world test target only; never hard-code channel-specific rules.
- Market data providers must stay behind interfaces; Toobit public klines is first provider.
- Do not use signed/private Toobit endpoints in market-data modules.
- Real Toobit market data tests must be explicitly guarded; default tests must use fakes/mocks.
- Mandatory real integration verification policy:
- Unit tests must never call real external services.
- Real integration tests must be skipped by default and run only with explicit guard env vars.
- All real credentials must come only from root `Triak_Trade/.env.local`.
- Never print secrets, tokens, API keys, API secrets, session strings, or full env values.
- Real checks must use the smallest safe request and print only non-secret summaries.
- Never execute live trades or withdrawals.
- Before finishing any task: run `ruff check .`, `mypy src`, `pytest`, run module dry-run CLI, and if a guard is enabled run the smallest real integration check, then manually inspect outputs/logs for safety and correctness.
- Telegram real check guard: `RUN_TELEGRAM_INTEGRATION_TESTS=1` (small fetch from `https://t.me/Tofan_Trade`, no session/credential exposure).
- AI gateway real check guard: `RUN_AI_GATEWAY_INTEGRATION_TESTS=1` (safe classification samples, no key exposure, ads/results must not be new signals).
- Toobit public market data guard: `RUN_TOOBIT_MARKETDATA_INTEGRATION_TESTS=1` (tiny `BTCUSDT` range, summary only).
- Toobit signed/private guard: `RUN_TOOBIT_SIGNED_INTEGRATION_TESTS=1` (read-only/safe endpoints first, no live order placement, no withdrawal endpoints).
- Demo execution guard: `RUN_TOOBIT_DEMO_EXECUTION_TESTS=1` and `EXECUTION_MODE=demo`; live mode remains blocked.
- Spot `orderTest` real verification guard: `RUN_TOOBIT_ORDERTEST_INTEGRATION_TESTS=1` and `EXECUTION_MODE=demo`.
- Never call `POST /api/v1/spot/order` in safety-validation phases; use `POST /api/v1/spot/orderTest` only.
- Admin approval bot authorization is username-based (`ADMIN_TELEGRAM_USERNAMES`), not numeric-id-first.
- `ADMIN_USER_IDS` remains deprecated/backward-compatible only.
- Admin decisions record approval/reject/watch only; they must not execute trades directly in admin module.
- Backtesting engine is simulation-only and must never execute real trades.
- Backtesting must use classifier interfaces/protocols (AI-ready), not regex internals directly.
