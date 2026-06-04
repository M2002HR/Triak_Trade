from __future__ import annotations

from pydantic import BaseModel, Field


class AppConfig(BaseModel):
    request_timeout_sec: int = 120


class AdminConfig(BaseModel):
    enabled: bool = False


class ImageDefaults(BaseModel):
    n: int = 1
    size: str = "1024x1024"
    quality: str = "medium"
    response_format: str = "b64_json"


class PollinationsConfig(BaseModel):
    base_url: str = "https://gen.pollinations.ai"
    api_keys: list[str] = Field(default_factory=list)
    default_image_model: str = "flux"
    use_proxy_2080: bool = False
    proxy_2080_url: str = "socks5://127.0.0.1:2080"
    trust_env_proxy: bool = False
    max_attempts_per_request: int = 6
    retry_status_codes: list[int] = Field(default_factory=list)
    retry_backoff_sec: float = 0.35
    cooldown_sec: float = 20.0


class Settings(BaseModel):
    app: AppConfig = Field(default_factory=AppConfig)
    pollinations: PollinationsConfig = Field(default_factory=PollinationsConfig)
    image_defaults: ImageDefaults = Field(default_factory=ImageDefaults)
    admin: AdminConfig = Field(default_factory=AdminConfig)
