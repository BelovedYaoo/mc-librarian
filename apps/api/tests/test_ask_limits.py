"""问答上限的取值域必须只有一处来源。

`max_steps` / `max_answer_tokens` / `max_context_tokens` 同时出现在三个模型里：
请求体 `AskRequest`、设置写入 `AskSettingsUpdate`、服务端配置 `Settings.ask_*`。
它们描述的是**同一条边界**，各自写死一份就会漂——曾经出现过「设置页能存 150、
`/v1/ask` 仍被 `le=12` 拒掉」，而且改的时候只改到其中一处，看不出问题在哪。

这组用例守住的就是这条：三个模型对同一个常量给出同样的接受/拒绝结果。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mc_librarian.config import Settings
from mc_librarian.schemas import (
    ASK_MAX_ANSWER_TOKENS,
    ASK_MAX_STEPS,
    AskRequest,
    AskSettingsUpdate,
)

SECRET = "test-secret-0123456789abcdef"


def ask_request(**overrides: object) -> AskRequest:
    return AskRequest.model_validate({"question": "刷怪上限", "version": "26.3", **overrides})


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    return Settings.model_validate(
        {"data_dir": tmp_path / "data", "secret": SECRET, **overrides}
    )


def test_max_steps_boundary_is_shared_by_all_three_models(tmp_path: Path) -> None:
    """三个模型都在 `ASK_MAX_STEPS` 处接受、在 +1 处拒绝。"""
    ask_request(max_steps=ASK_MAX_STEPS)
    AskSettingsUpdate(max_steps=ASK_MAX_STEPS)
    make_settings(tmp_path, ask_max_steps=ASK_MAX_STEPS)

    with pytest.raises(ValidationError):
        ask_request(max_steps=ASK_MAX_STEPS + 1)

    with pytest.raises(ValidationError):
        AskSettingsUpdate(max_steps=ASK_MAX_STEPS + 1)

    with pytest.raises(ValidationError):
        make_settings(tmp_path, ask_max_steps=ASK_MAX_STEPS + 1)


def test_max_answer_tokens_boundary_is_shared(tmp_path: Path) -> None:
    ask_request(max_answer_tokens=ASK_MAX_ANSWER_TOKENS)
    AskSettingsUpdate(max_answer_tokens=ASK_MAX_ANSWER_TOKENS)
    make_settings(tmp_path, ask_max_answer_tokens=ASK_MAX_ANSWER_TOKENS)

    with pytest.raises(ValidationError):
        ask_request(max_answer_tokens=ASK_MAX_ANSWER_TOKENS + 1)

    with pytest.raises(ValidationError):
        AskSettingsUpdate(max_answer_tokens=ASK_MAX_ANSWER_TOKENS + 1)

    with pytest.raises(ValidationError):
        make_settings(tmp_path, ask_max_answer_tokens=ASK_MAX_ANSWER_TOKENS + 1)


def test_default_steps_fit_inside_the_bound(tmp_path: Path) -> None:
    """默认值必须落在自己的上限内（改上限时最容易忘的就是这一条）。"""
    default = make_settings(tmp_path).ask_max_steps

    assert 1 <= default <= ASK_MAX_STEPS
