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
