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
