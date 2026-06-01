"""CLI entrypoint."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import typer

from triak_trade import __version__
from triak_trade.agents.channel_agent import ChannelAgent
from triak_trade.agents.clock import FakeClock
from triak_trade.config.settings import Settings, get_settings
from triak_trade.core.health import run_health_checks
from triak_trade.core.logging import configure_logging
from triak_trade.db.engine import build_engine_from_settings
from triak_trade.domain.models import RawTelegramMessage
from triak_trade.parsing.normalizer import MessageNormalizer
from triak_trade.parsing.regex_parser import RegexSignalParser
from triak_trade.parsing.validator import ParsedSignalValidator

app = typer.Typer(no_args_is_help=True)


def _load_settings() -> Settings:
    settings = get_settings()
    configure_logging(settings)
    return settings


@app.command("version")
def version_cmd() -> None:
    """Show app version."""
    typer.echo(__version__)


@app.command("health")
def health_cmd(
    include_services: bool = typer.Option(False, help="Include DB/Redis checks."),
) -> None:
    """Run health checks."""
    settings = _load_settings()
    result = run_health_checks(settings=settings, include_services=include_services)
    typer.echo(json.dumps(asdict(result), indent=2, sort_keys=True))


@app.command("config-check")
def config_check_cmd() -> None:
    """Validate config and print safe status."""
    _load_settings()
    typer.echo("Configuration is valid")


@app.command("db-check")
def db_check_cmd() -> None:
    """Build DB engine from config without connecting."""
    settings = _load_settings()
    engine = build_engine_from_settings(settings)
    typer.echo(f"DB engine configured (dialect={engine.dialect.name})")


@app.command("parse-message")
def parse_message_cmd(message: str) -> None:
    """Normalize, parse, and validate a single message safely."""
    settings = _load_settings()

    raw = RawTelegramMessage(
        channel_id="cli",
        channel_username=None,
        message_id=1,
        text=message,
        date=datetime.now(timezone.utc),
        edited_at=None,
        reply_to_msg_id=None,
    )
    normalizer = MessageNormalizer()
    parser = RegexSignalParser()
    validator = ParsedSignalValidator()

    normalized = normalizer.normalize(raw)
    parsed = parser.parse(normalized)
    ok, errors = validator.validate_for_proposal(
        parsed,
        max_leverage=settings.MAX_LEVERAGE,
        require_stop_loss=settings.REQUIRE_STOP_LOSS,
    )

    payload = {
        "normalized_text": normalized.normalized_text,
        "detected_symbols": normalized.detected_symbols,
        "detected_keywords": normalized.detected_keywords,
        "parsed": parsed.model_dump(mode="json"),
        "proposal_valid": ok,
        "validation_errors": errors,
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("agent-dry-run")
def agent_dry_run_cmd() -> None:
    """Run a deterministic in-memory channel agent simulation."""
    settings = _load_settings()
    start = datetime.now(timezone.utc)
    clock = FakeClock(start)
    agent = ChannelAgent(channel_id="dry-run-channel", settings=settings, clock=clock)

    sequence = [
        (0, "BTCUSDT LONG Entry: 68000 - 68200 SL: 67400 TP: 69000 / 70000 Leverage: 5x"),
        (30, "SL: 67400"),
        (60, "TP: 69000 / 70000"),
        (90, "Join VIP now!"),
    ]

    immediate_actions: list[dict[str, object]] = []
    for idx, (offset_sec, text) in enumerate(sequence, start=1):
        clock.advance(seconds=offset_sec - (0 if idx == 1 else sequence[idx - 2][0]))
        message = RawTelegramMessage(
            channel_id="dry-run-channel",
            channel_username="dry",
            message_id=idx,
            text=text,
            date=clock.now(),
            edited_at=None,
            reply_to_msg_id=1 if idx in {2, 3} else None,
        )
        for action in agent.ingest_message(message):
            immediate_actions.append(action.model_dump(mode="json"))

    clock.advance(seconds=max(0, settings.SIGNAL_CONSOLIDATION_SECONDS - 90))
    tick_actions = [action.model_dump(mode="json") for action in agent.tick(clock.now())]

    snapshot = agent.get_context_snapshot()
    payload = {
        "pending_signal_ids": snapshot.get("pending_signal_ids", []),
        "signal_statuses": snapshot.get("signals", {}),
        "immediate_actions": immediate_actions,
        "tick_actions": tick_actions,
        "counts": {
            "immediate_actions": len(immediate_actions),
            "tick_actions": len(tick_actions),
        },
        "safety": {"requires_admin_approval_default": True, "no_execution": True},
    }
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))
