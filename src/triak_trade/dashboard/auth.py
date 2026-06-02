"""Dashboard token auth."""

from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer

from triak_trade.config.settings import Settings


class DashboardAuth:
    cookie_name = "triak_dashboard_session"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        secret = settings.DASHBOARD_SESSION_SECRET.get_secret_value()
        self.serializer = URLSafeSerializer(secret, salt="dashboard-session") if secret else None

    def redirect_if_needed(self, request: Request) -> RedirectResponse | None:
        if not self.settings.DASHBOARD_AUTH_ENABLED:
            return None
        if self.is_authenticated(request):
            return None
        next_path = quote(str(request.url.path or "/"), safe="/?=&")
        return RedirectResponse(
            url=f"/login?next={next_path}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    def require_api(self, request: Request) -> None:
        if not self.settings.DASHBOARD_AUTH_ENABLED:
            return
        if self.is_authenticated(request):
            return
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")

    def is_authenticated(self, request: Request) -> bool:
        expected = self.settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
        if not expected:
            return False
        provided = request.headers.get("X-Triak-Admin-Token") or request.query_params.get("token")
        if provided is not None and secrets.compare_digest(provided, expected):
            return True
        return self._has_valid_session_cookie(request)

    def validate_login_token(self, token: str) -> bool:
        expected = self.settings.DASHBOARD_ADMIN_TOKEN.get_secret_value()
        return bool(expected and secrets.compare_digest(token, expected))

    def set_session_cookie(self, response: Response) -> None:
        if self.serializer is None:
            return
        value = self.serializer.dumps({"scope": "dashboard"})
        response.set_cookie(
            key=self.cookie_name,
            value=value,
            httponly=True,
            samesite="lax",
            secure=False,
        )

    def clear_session_cookie(self, response: Response) -> None:
        response.delete_cookie(self.cookie_name)

    def _has_valid_session_cookie(self, request: Request) -> bool:
        if self.serializer is None:
            return False
        cookie = request.cookies.get(self.cookie_name)
        if not cookie:
            return False
        try:
            payload = self.serializer.loads(cookie)
        except BadSignature:
            return False
        return isinstance(payload, dict) and payload.get("scope") == "dashboard"


def token_present(settings: Settings) -> bool:
    return bool(settings.DASHBOARD_ADMIN_TOKEN.get_secret_value())


def session_secret_present(settings: Settings) -> bool:
    return bool(settings.DASHBOARD_SESSION_SECRET.get_secret_value())
