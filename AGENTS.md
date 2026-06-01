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
