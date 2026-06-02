"""Dashboard token auth."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

from triak_trade.config.settings import Settings


class DashboardAuth:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def require(self, request: Request) -> None:
        if not self.settings.DASHBOARD_AUTH_ENABLED:
            return
        expected = self.settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
        if not expected:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="dashboard admin token is not configured",
            )
        provided = request.headers.get("X-Triak-Admin-Token") or request.query_params.get("token")
        if provided is None or not secrets.compare_digest(provided, expected):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")


def token_present(settings: Settings) -> bool:
    return bool(settings.DASHBOARD_ADMIN_TOKEN.get_secret_value())


def session_secret_present(settings: Settings) -> bool:
    return bool(settings.DASHBOARD_SESSION_SECRET.get_secret_value())
