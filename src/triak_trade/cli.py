"""CLI entrypoint."""

from __future__ import annotations

import json
from dataclasses import asdict

import typer

from triak_trade import __version__
from triak_trade.config.settings import Settings, get_settings
from triak_trade.core.health import run_health_checks
from triak_trade.core.logging import configure_logging

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
