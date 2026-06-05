"""Ajil gateway bootstrap helpers for optional provider modules."""

from __future__ import annotations

from pathlib import Path

_POLLINATIONS_PACKAGE_PARTS = ("modules", "pollinations_proxy", "api", "app")


def pollinations_module_exists(gateway_root: Path) -> bool:
    return (gateway_root.joinpath(*_POLLINATIONS_PACKAGE_PARTS, "config.py")).exists()


def prepare_optional_provider_stubs(
    *,
    gateway_root: Path,
    stub_root: Path,
    pollinations_enabled: bool,
) -> list[str]:
    if pollinations_enabled or pollinations_module_exists(gateway_root):
        return []

    package_root = stub_root.joinpath(*_POLLINATIONS_PACKAGE_PARTS)
    package_root.mkdir(parents=True, exist_ok=True)
    for directory in [
        stub_root / "modules",
        stub_root / "modules" / "pollinations_proxy",
        stub_root / "modules" / "pollinations_proxy" / "api",
        package_root,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
        init_file = directory / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

    (package_root / "config.py").write_text(
        """
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppConfig:
    request_timeout_sec: int = 120


@dataclass
class PollinationsConfig:
    base_url: str = ""
    api_keys: list[str] | None = None
    default_image_model: str = "flux"
    use_proxy_2080: bool = False
    proxy_2080_url: str = ""
    trust_env_proxy: bool = False
    max_attempts_per_request: int = 1
    retry_status_codes: list[int] | None = None
    retry_backoff_sec: float = 0.0
    cooldown_sec: int = 0


@dataclass
class ImageDefaults:
    n: int = 1
    size: str = "1024x1024"
    quality: str = "standard"
    response_format: str = "b64_json"


@dataclass
class AdminConfig:
    enabled: bool = False


@dataclass
class Settings:
    app: AppConfig
    pollinations: PollinationsConfig
    image_defaults: ImageDefaults
    admin: AdminConfig
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (package_root / "services.py").write_text(
        """
from __future__ import annotations


class PollinationsProxyService:
    def __init__(self, settings) -> None:
        self.settings = settings

    async def close(self) -> None:
        return None

    async def generate_image(self, body):
        return 501, {"error": {"message": "pollinations provider unavailable"}}, {}

    async def list_free_image_models(self, force_refresh: bool = False):
        return []

    async def generate_image_get_public(self, prompt: str, params):
        return 501, {"error": "pollinations provider unavailable"}, {}, "json"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return [str(stub_root)]
