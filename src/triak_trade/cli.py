"""CLI entrypoint."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import typer

from triak_trade import __version__
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
