"""数据根配置测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from mc_librarian.config import (
    DATA_DIR_ENV,
    Settings,
    default_data_dir,
    find_repo_root,
)


def test_default_data_dir_is_outside_repo() -> None:
    root = find_repo_root()
    assert root is not None, "测试应在仓库内运行（package.json + apps/api 哨兵可见）"

    resolved = default_data_dir()

    assert resolved == root.parent / "mc-librarian-data"
    assert root not in resolved.parents, "数据根不能落在仓库内"


def test_default_data_dir_falls_back_to_home(tmp_path: Path) -> None:
    """没有仓库哨兵时（例如装进 site-packages）退回 ~/mc-librarian-data。"""
    stranger = tmp_path / "no-sentinels-here"
    stranger.mkdir()

    assert find_repo_root(stranger) is None
    assert default_data_dir(stranger) == Path.home() / "mc-librarian-data"


def test_data_dir_env_var_overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    custom = tmp_path / "elsewhere"
    monkeypatch.setenv(DATA_DIR_ENV, str(custom))

    settings = Settings()

    assert settings.data_dir == custom.resolve()
    assert settings.data_dir_from_env is True


def test_data_dir_default_is_not_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DATA_DIR_ENV, raising=False)

    settings = Settings()

    assert settings.data_dir_from_env is False


def test_settings_host_and_port_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MC_LIBRARIAN_HOST", raising=False)
    monkeypatch.delenv("MC_LIBRARIAN_PORT", raising=False)

    settings = Settings()

    assert settings.host == "127.0.0.1"
    assert settings.port == 8787
