from __future__ import annotations

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app
from triak_trade.dashboard.local_client import LocalASGIClient


def test_approval_decision_records_safe_placeholder() -> None:
    app = create_dashboard_app(Settings(_env_file=None, DASHBOARD_ADMIN_TOKEN="test-token"))
    client = LocalASGIClient(app)

    response = client.post(
        "/approvals/action_1/approve",
        headers={"X-Triak-Admin-Token": "test-token"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/approvals")
