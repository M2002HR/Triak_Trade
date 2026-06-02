from __future__ import annotations

from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app


def test_approval_decision_records_safe_placeholder() -> None:
    app = create_dashboard_app(Settings(_env_file=None, DASHBOARD_ADMIN_TOKEN="test-token"))
    client = TestClient(app)

    response = client.post(
        "/approvals/action_1/approve",
        headers={"X-Triak-Admin-Token": "test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/approvals")
