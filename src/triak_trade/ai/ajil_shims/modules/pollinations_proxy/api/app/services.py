from __future__ import annotations

from typing import Any


class PollinationsProxyService:
    def __init__(self, settings: Any) -> None:
        self.settings = settings

    async def close(self) -> None:
        return None

    async def generate_image(
        self,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        return 503, {"error": {"message": "pollinations shim disabled"}}, {}

    async def list_free_image_models(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        return []

    async def generate_image_get_public(
        self,
        *,
        prompt: str,
        params: dict[str, Any],
    ) -> tuple[int, dict[str, Any], dict[str, str], str]:
        return 503, {"error": {"message": "pollinations shim disabled"}}, {}, "b64_json"
