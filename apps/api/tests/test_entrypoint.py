"""入口层的配置护栏：配置不合法时要「一句话 + 退出码」，而不是 pydantic traceback。"""

from __future__ import annotations

import pytest

from mc_librarian.__main__ import main
from mc_librarian.config import get_settings


def test_main_exits_cleanly_when_production_cookie_insecure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MC_LIBRARIAN_ENV", "production")
    monkeypatch.delenv("MC_LIBRARIAN_COOKIE_SECURE", raising=False)

    # `get_settings` 是 lru_cache 单例，先清掉才能读到刚设的环境变量。
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as excinfo:
            main()
    finally:
        get_settings.cache_clear()

    assert excinfo.value.code == 2

    stderr = capsys.readouterr().err
    assert "配置错误" in stderr
    assert "MC_LIBRARIAN_COOKIE_SECURE" in stderr
