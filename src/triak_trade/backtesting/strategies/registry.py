"""Strategy registry and config-file loader."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy

_STRATEGY_CLASSES: dict[str, type] = {
    "default_risk_managed": DefaultRiskManagedStrategy,
}

_DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "active_strategy": "default_risk_managed",
    "strategies": {
        "default_risk_managed": {
            "no_sl_loss_pct": "100",
            "risk_free_on_first_tp": True,
            "tp_close_fractions": ["0.35", "0.40", "0.50"],
        }
    },
}


def load_strategy_from_dict(config: dict[str, Any]) -> TradeStrategy:
    """
    Build a TradeStrategy from a config dict.

    Expected shape::

        {
          "active_strategy": "default_risk_managed",
          "strategies": {
            "default_risk_managed": {
              "no_sl_loss_pct": "100",
              "risk_free_on_first_tp": true,
              "tp_close_fractions": ["0.35", "0.40", "0.50"]
            }
          }
        }
    """
    active = config.get("active_strategy", "default_risk_managed")
    strategy_cfg = config.get("strategies", {}).get(active, {})

    cls = _STRATEGY_CLASSES.get(active)
    if cls is None:
        raise ValueError(
            f"Unknown strategy '{active}'. "
            f"Available: {list(_STRATEGY_CLASSES)}"
        )

    if cls is DefaultRiskManagedStrategy:
        fractions_raw = strategy_cfg.get("tp_close_fractions", ["0.35", "0.40", "0.50"])
        return DefaultRiskManagedStrategy(
            no_sl_loss_pct=Decimal(str(strategy_cfg.get("no_sl_loss_pct", "100"))),
            risk_free_on_first_tp=bool(strategy_cfg.get("risk_free_on_first_tp", True)),
            tp_close_fractions=[Decimal(str(f)) for f in fractions_raw],
        )

    raise ValueError(f"Strategy '{active}' has no loader registered.")


def load_strategy(config_path: Path | str | None = None) -> TradeStrategy:
    """
    Load a strategy from a YAML config file.

    Falls back to the built-in defaults when the file is absent, pyyaml is not
    installed, or the file is unparseable.
    The path defaults to ``config/strategies.yaml`` relative to the project root.
    """
    if config_path is None:
        # Project root is parents[4] from this file:
        # registry.py → strategies/ → backtesting/ → triak_trade/ → src/ → project root
        here = Path(__file__).resolve()
        project_root = here.parents[4]
        config_path = project_root / "config" / "strategies.yaml"

    path = Path(config_path)
    if not path.exists():
        return load_strategy_from_dict(_DEFAULT_STRATEGY_CONFIG)

    try:
        import yaml  # type: ignore[import-untyped]

        with path.open() as fh:
            cfg = yaml.safe_load(fh) or {}
        return load_strategy_from_dict(cfg)
    except Exception:
        return load_strategy_from_dict(_DEFAULT_STRATEGY_CONFIG)
