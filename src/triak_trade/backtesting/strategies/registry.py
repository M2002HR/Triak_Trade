"""Strategy registry and config-file loader."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any

from triak_trade.backtesting.strategies.base import TradeStrategy
from triak_trade.backtesting.strategies.default_risk import DefaultRiskManagedStrategy
from triak_trade.backtesting.strategies.trailing_tp import TrailingTakeProfitStrategy

_STRATEGY_CLASSES: dict[str, type] = {
    "default_risk_managed": DefaultRiskManagedStrategy,
    "tp_trailing_risk_managed": TrailingTakeProfitStrategy,
}

_DEFAULT_STRATEGY_CONFIG: dict[str, Any] = {
    "active_strategy": "default_risk_managed",
    "strategies": {
        "default_risk_managed": {
            "no_sl_loss_pct": "100",
            "risk_free_on_first_tp": True,
            "tp_close_fractions": ["0.35", "0.40", "0.50"],
        },
        "tp_trailing_risk_managed": {
            "no_sl_loss_pct": "100",
            "risk_free_on_first_tp": True,
            "tp_close_fractions": ["0.35", "0.40", "0.50"],
        },
    },
}

_STRATEGY_DESCRIPTIONS: dict[str, str] = {
    "default_risk_managed": (
        "Synthetic stop for missing SL, optional breakeven after TP1, "
        "and partial exits across the TP ladder."
    ),
    "tp_trailing_risk_managed": (
        "Default risk-managed behavior plus stop trailing to the previous "
        "take-profit after each new TP hit."
    ),
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

    if cls in {DefaultRiskManagedStrategy, TrailingTakeProfitStrategy}:
        fractions_raw = strategy_cfg.get("tp_close_fractions", ["0.35", "0.40", "0.50"])
        return cls(
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
        yaml = import_module("yaml")

        with path.open() as fh:
            cfg = yaml.safe_load(fh) or {}
        return load_strategy_from_dict(cfg)
    except Exception:
        return load_strategy_from_dict(_DEFAULT_STRATEGY_CONFIG)


def list_available_strategies(
    config_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    config = _load_strategy_config_dict(_resolve_config_path(config_path))
    active = str(config.get("active_strategy", "default_risk_managed"))
    strategies_cfg = config.get("strategies", {})
    items: list[dict[str, Any]] = []
    for strategy_key, strategy_class in _STRATEGY_CLASSES.items():
        strategy = load_strategy_from_dict(
            {
                "active_strategy": strategy_key,
                "strategies": {
                    strategy_key: strategies_cfg.get(strategy_key, {}),
                },
            }
        )
        items.append(
            {
                "key": strategy_key,
                "name": strategy.name,
                "class_name": strategy_class.__name__,
                "active": strategy_key == active,
                "description": _STRATEGY_DESCRIPTIONS.get(strategy_key, strategy_key),
                "parameters": _serialize_strategy_parameters(strategy),
            }
        )
    return items


def build_strategy_from_key(
    strategy_key: str,
    config_path: Path | str | None = None,
) -> TradeStrategy:
    config = _load_strategy_config_dict(_resolve_config_path(config_path))
    return load_strategy_from_dict(
        {
            "active_strategy": strategy_key,
            "strategies": config.get("strategies", {}),
        }
    )


def _serialize_strategy_parameters(strategy: TradeStrategy) -> dict[str, Any]:
    raw = (
        asdict(strategy)
        if is_dataclass(strategy)
        else dict(getattr(strategy, "__dict__", {}))
    )
    normalized: dict[str, Any] = {}
    for key, value in raw.items():
        if isinstance(value, Decimal):
            normalized[key] = str(value)
        elif isinstance(value, list):
            normalized[key] = [
                str(item) if isinstance(item, Decimal) else item
                for item in value
            ]
        else:
            normalized[key] = value
    return normalized


def _resolve_config_path(config_path: Path | str | None = None) -> Path:
    if config_path is None:
        here = Path(__file__).resolve()
        project_root = here.parents[4]
        return project_root / "config" / "strategies.yaml"
    return Path(config_path)


def _load_strategy_config_dict(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return _DEFAULT_STRATEGY_CONFIG
    try:
        yaml = import_module("yaml")

        with config_path.open() as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return _DEFAULT_STRATEGY_CONFIG
