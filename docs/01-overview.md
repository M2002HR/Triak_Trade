# 01 - Project Overview

## Purpose

Triak_Trade is a modular signal-intelligence platform centered on Telegram message processing, AI-assisted classification, backtesting, dashboard-driven monitoring, and guarded demo/live execution workflows.

The intended parsing/classification end-state is AI-driven through Ajil Unified AI Gateway. Regex parsing remains a fallback and safety layer, not the final decision engine.

## Main Technology Stack

- Python 3.10+
- Pydantic v2 and `pydantic-settings`
- Typer CLI
- FastAPI + Jinja + WebSockets dashboard
- SQLAlchemy + Alembic + MySQL
- Redis
- Telethon behind interfaces
- `httpx` for service calls
- `structlog` and JSON/human logging

## Repository Map

| Module | Role |
|-------|------|
| `agents/` | Channel context, consolidation logic, and action proposals |
| `ai/` | Ajil gateway client, runtime management, prompts, and AI classifier |
| `backtesting/` | Fixture engine, real pipeline, simulator, scoring, strategies, and reports |
| `cache/` | Redis support |
| `config/` | Runtime settings sourced from root `.env.local` |
| `core/` | Logging, time handling, formatting, health checks, and symbol helpers |
| `dashboard/` | Local dashboard, backtest runtime, live/demo workspace, and settings views |
| `db/` | Engines, models, sessions, and repositories |
| `deployment/` | Ajil bootstrap and runtime-env helpers |
| `domain/` | Core Pydantic models and enums |
| `exchange/toobit/` | Toobit public/signed/demo-safe adapters |
| `live_trading/` | Demo/live session orchestration and state |
| `market_data/` | Binance public, Toobit public, composite providers, and candle cache |
| `observability/` | Processing audit, redaction, Telegram log channel, and event bus |
| `parsing/` | Text normalization, regex parsing, and validation |
| `telegram/` | Telegram client interfaces, history sync, and listener plumbing |
| `verification/` | Safe and guarded real-system verification |

## User-Facing Capabilities

- Safe parsing and validation of signal-like Telegram messages
- Dry-run agent simulation
- AI gateway runtime checks and dry-run classification
- Telegram history dry-runs
- Public market-data dry-runs
- Fixture backtesting and guarded real backtesting
- Dashboard-based backtest execution and report browsing
- Dashboard live/demo session monitoring
- Processing-audit formatting and guarded log-channel delivery
- Verification reports with redacted summaries

## Non-Negotiable Project Rules

These come from `AGENTS.md` and shape the implementation:

1. Each module should be independently testable.
2. Unit tests must not call real external services.
3. Integration tests must be explicitly guard-gated.
4. Financial values use `Decimal`, not `float`.
5. Secrets must never be printed or committed.
6. Backtesting is simulation-only and must not execute trades.
7. Real integrations must live behind interfaces/adapters.
8. Runtime configuration comes from the root `.env.local`.
9. Live sessions require `LIVE_TRADING_LIVE_MODE_ENABLED=true`.
10. If AI is unavailable, the system must say so explicitly instead of pretending AI was used.

## Important Current Reality

- The Ajil gateway exists as a git submodule under `external/Ajil_Unified_AI_Gateway`.
- The repository already contains dashboard and live/demo trading code, not just backtesting.
- The CLI surface is broader than the original README used to imply, but it no longer includes an admin-bot command set.
- Real backtesting is available, but its readiness gate is intentionally strict and still somewhat test-flag-shaped.
