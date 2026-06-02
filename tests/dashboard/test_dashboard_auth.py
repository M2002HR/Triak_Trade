from __future__ import annotations

from fastapi.testclient import TestClient

from triak_trade.config.settings import Settings
from triak_trade.dashboard.app import create_dashboard_app


def settings() -> Settings:
    return Settings(
        _env_file=None,
        DASHBOARD_ADMIN_TOKEN="test-token",
        DASHBOARD_SESSION_SECRET="session-secret",
    )


def test_dashboard_unauthorized_request_blocked() -> None:
    client = TestClient(create_dashboard_app(settings()))
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")


def test_dashboard_authorized_request_works() -> None:
    client = TestClient(create_dashboard_app(settings()))
    response = client.get("/", headers={"X-Triak-Admin-Token": "test-token"})
    assert response.status_code == 200
    assert "Triak Trade Management Dashboard" in response.text


def test_dashboard_query_token_allowed_for_local_dev() -> None:
    client = TestClient(create_dashboard_app(settings()))
    response = client.get("/?token=test-token")
    assert response.status_code == 200


def test_dashboard_login_form_sets_cookie() -> None:
    client = TestClient(create_dashboard_app(settings()))
    response = client.post(
        "/login",
        data={"token": "test-token", "next_path": "/"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "triak_dashboard_session=" in response.headers["set-cookie"]
