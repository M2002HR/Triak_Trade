from __future__ import annotations

import os

import pytest


def test_backtest_integration_guarded() -> None:
    if os.getenv("RUN_BACKTEST_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")
    assert True
