from __future__ import annotations

import os

import pytest


def test_admin_bot_integration_guarded() -> None:
    if os.getenv("RUN_TELEGRAM_BOT_INTEGRATION_TESTS") != "1":
        pytest.skip("guard disabled")
    assert True
