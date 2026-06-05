from __future__ import annotations

from pathlib import Path

from triak_trade.deployment.ajil_bootstrap import (
    pollinations_module_exists,
    prepare_optional_provider_stubs,
)


def test_pollinations_module_exists_detects_config(tmp_path: Path) -> None:
    gateway_root = tmp_path / "gateway"
    config_path = gateway_root / "modules" / "pollinations_proxy" / "api" / "app" / "config.py"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("# stub\n", encoding="utf-8")
    assert pollinations_module_exists(gateway_root) is True


def test_prepare_optional_provider_stubs_skips_when_provider_enabled(tmp_path: Path) -> None:
    paths = prepare_optional_provider_stubs(
        gateway_root=tmp_path / "gateway",
        stub_root=tmp_path / "stubs",
        pollinations_enabled=True,
    )
    assert paths == []
    assert not (tmp_path / "stubs").exists()


def test_prepare_optional_provider_stubs_creates_importable_layout(tmp_path: Path) -> None:
    stub_root = tmp_path / "stubs"
    paths = prepare_optional_provider_stubs(
        gateway_root=tmp_path / "gateway",
        stub_root=stub_root,
        pollinations_enabled=False,
    )
    assert paths == [str(stub_root)]
    assert (stub_root / "modules" / "pollinations_proxy" / "api" / "app" / "config.py").exists()
    assert (stub_root / "modules" / "pollinations_proxy" / "api" / "app" / "services.py").exists()
