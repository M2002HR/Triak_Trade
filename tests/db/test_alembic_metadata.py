from __future__ import annotations

import importlib.util
from pathlib import Path

from triak_trade.db.base import Base


def test_alembic_metadata_imports() -> None:
    env_path = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
    spec = importlib.util.spec_from_file_location("triak_trade_alembic_env", env_path)
    assert spec is not None
    assert spec.loader is not None
    env_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(env_module)
    assert env_module.target_metadata is Base.metadata
