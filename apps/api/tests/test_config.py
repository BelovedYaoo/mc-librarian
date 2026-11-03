"""数据根配置测试。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from conftest import TEST_SECRET
from mc_librarian.config import (
    ALERTS_DAILY_COST_THRESHOLD_ENV,
    ALERTS_DAILY_TOKEN_THRESHOLD_ENV,
    ALERTS_WEBHOOK_URL_ENV,
    DATA_DIR_ENV,
    DATA_DIR_NAME,
    Settings,
    default_data_dir,
    find_repo_root,
    legacy_env_warnings,
    pricing_configured,
    resolve_alerts_config,
    resolve_data_dir,
)
from mc_librarian.main import create_app


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


def test_relative_data_dir_is_resolved_against_repo_root() -> None:
    """相对路径按仓库根解析，而不是按进程 cwd（dev 时 cwd 是 apps/api）。"""
    root = find_repo_root()
    assert root is not None

    resolved = resolve_data_dir(Path("..") / DATA_DIR_NAME)

    assert resolved == (root.parent / DATA_DIR_NAME).resolve()


def test_relative_data_dir_inside_repo_is_rejected() -> None:
    root = find_repo_root()
    assert root is not None

    with pytest.raises(ValidationError) as excinfo:
        Settings(data_dir=Path("data"))

    assert "数据根不能落在仓库内" in str(excinfo.value)
    assert str(root) in str(excinfo.value)


def test_absolute_data_dir_inside_repo_is_rejected() -> None:
    root = find_repo_root()
    assert root is not None

    with pytest.raises(ValidationError):
        Settings(data_dir=root / "data")


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


def test_cookie_secure_defaults_to_false_for_local_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认必须是 false：本地 http 下 Secure 会让浏览器直接丢掉 cookie。"""
    monkeypatch.delenv("MC_LIBRARIAN_COOKIE_SECURE", raising=False)

    assert Settings().cookie_secure is False


def test_cookie_secure_env_var_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MC_LIBRARIAN_COOKIE_SECURE", "true")

    assert Settings().cookie_secure is True


def test_session_ttl_defaults_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MC_LIBRARIAN_SESSION_TTL_HOURS", raising=False)
    assert Settings().session_ttl_hours == 168

    monkeypatch.setenv("MC_LIBRARIAN_SESSION_TTL_HOURS", "24")
    assert Settings().session_ttl_hours == 24


def test_env_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MC_LIBRARIAN_ENV", raising=False)

    assert Settings().env == "dev"


def test_index_enabled_defaults_true_and_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """索引构建闸门默认 true（本地开箱可用）；节点用环境变量关掉。"""
    monkeypatch.delenv("MC_LIBRARIAN_INDEX_ENABLED", raising=False)
    assert Settings().index_enabled is True

    monkeypatch.setenv("MC_LIBRARIAN_INDEX_ENABLED", "false")
    assert Settings().index_enabled is False


def test_production_with_insecure_cookie_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """生产护栏：忘了开 Secure，启动就该直接挂掉，而不是静默跑 http。"""
    monkeypatch.setenv("MC_LIBRARIAN_ENV", "production")
    monkeypatch.delenv("MC_LIBRARIAN_COOKIE_SECURE", raising=False)

    with pytest.raises(ValidationError) as excinfo:
        Settings()

    assert "MC_LIBRARIAN_COOKIE_SECURE=true" in str(excinfo.value)


def test_production_with_insecure_cookie_fails_fast_as_kwargs(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="Secure"):
        Settings(data_dir=tmp_path / "data", env="production", cookie_secure=False)


def test_production_with_secure_cookie_is_accepted(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        env="production",
        cookie_secure=True,
    )

    assert settings.env == "production"
    assert settings.cookie_secure is True


def test_unknown_env_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path / "data", env="staging")  # type: ignore[arg-type]


def test_alerts_default_to_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """#12：默认没有 Webhook、阈值为 0（不告警）。"""
    for name in (
        ALERTS_WEBHOOK_URL_ENV,
        ALERTS_DAILY_TOKEN_THRESHOLD_ENV,
        ALERTS_DAILY_COST_THRESHOLD_ENV,
    ):
        monkeypatch.delenv(name, raising=False)

    config = resolve_alerts_config(Settings(data_dir=tmp_path / "data"))

    assert config.webhook_url is None
    assert config.daily_token_threshold == 0
    assert config.daily_cost_threshold_usd == 0.0


def test_resolve_alerts_config_reads_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """阈值与 Webhook 地址从 `MC_LIBRARIAN_ALERTS_*` 读（库里有覆盖时优先，见 test_settings）。"""
    monkeypatch.setenv(ALERTS_WEBHOOK_URL_ENV, "https://hook.example/alerts")
    monkeypatch.setenv(ALERTS_DAILY_TOKEN_THRESHOLD_ENV, "1000000")
    monkeypatch.setenv(ALERTS_DAILY_COST_THRESHOLD_ENV, "5.5")

    config = resolve_alerts_config(Settings(data_dir=tmp_path / "data"))

    assert config.webhook_url == "https://hook.example/alerts"
    assert config.daily_token_threshold == 1_000_000
    assert config.daily_cost_threshold_usd == 5.5


def test_pricing_configured_needs_a_positive_price(tmp_path: Path) -> None:
    """`cost_configured` 的口径：只有真的配了正单价才算「成本可读」，否则页面要标注只统计 token。"""
    settings = Settings(data_dir=tmp_path / "data")

    assert pricing_configured(settings) is False

    settings.llm_input_price_per_m = 0.0

    assert pricing_configured(settings) is False

    settings.llm_input_price_per_m = 0.27

    assert pricing_configured(settings) is True


def test_zero_thresholds_are_accepted_and_disable_alerts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """阈值 0 合法且表示不告警：环境变量写 0 不该让启动失败（与文档「0 = 关闭」一致）。"""
    monkeypatch.setenv(ALERTS_WEBHOOK_URL_ENV, "https://hook.example/alerts")
    monkeypatch.setenv(ALERTS_DAILY_TOKEN_THRESHOLD_ENV, "0")
    monkeypatch.setenv(ALERTS_DAILY_COST_THRESHOLD_ENV, "0")

    config = resolve_alerts_config(Settings(data_dir=tmp_path / "data"))

    assert config.daily_token_threshold == 0
    assert config.daily_cost_threshold_usd == 0.0

    # 负数仍被拒：语义是「0 = 关闭」，不是「随便给个非正数」。
    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path / "data", alerts_daily_token_threshold=-1)


def test_legacy_alerts_env_names_warn_at_startup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """旧名 `MC_LIBRARIAN_ALERT_*` 被 `extra="ignore"` 无声忽略，启动时必须 warn 出旧名与新名。"""
    monkeypatch.setenv("MC_LIBRARIAN_ALERT_WEBHOOK_URL", "https://old.invalid/hook")

    warnings = legacy_env_warnings()

    assert any("MC_LIBRARIAN_ALERT_WEBHOOK_URL" in warning for warning in warnings)

    settings = Settings(data_dir=tmp_path / "data", secret=TEST_SECRET)

    with caplog.at_level(logging.WARNING, logger="mc_librarian"), TestClient(create_app(settings)):
        pass

    messages = [record.getMessage() for record in caplog.records]

    assert any(
        "MC_LIBRARIAN_ALERT_WEBHOOK_URL" in message
        and "MC_LIBRARIAN_ALERTS_WEBHOOK_URL" in message
        and "旧名已失效" in message
        for message in messages
    )
