"""运行时配置。

数据根由 `MC_LIBRARIAN_DATA_DIR` 指定；未指定时**无条件**落在仓库之外的同级目录
`<repo 的父目录>/mc-librarian-data`，找不到仓库哨兵时退回 `~/mc-librarian-data`，
与 `docs/00-overview.md` §4 的数据布局一致（D9：数据根独立、不入库）。
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from mc_librarian.schemas import JAVA_XMX_PATTERN

DATA_DIR_ENV: Final = "MC_LIBRARIAN_DATA_DIR"
DATA_DIR_NAME: Final = "mc-librarian-data"

# 运行环境。`production` 下会给会话 cookie 加硬性护栏（见 `Settings._guard_production`）。
ENV_DEV: Final = "dev"
ENV_PRODUCTION: Final = "production"

# 会话 cookie 名，与 docs/openapi.yaml 的 `components.securitySchemes.cookieAuth` 一致。
SESSION_COOKIE_NAME: Final = "mc_librarian_session"

# 主密钥（AES-GCM 加密 `settings` 表里的密钥值）。缺失/过短时服务启动失败，见
# `secretbox.require_secret`——自动生成并落盘会让重启后旧密文解不开。
SECRET_ENV: Final = "MC_LIBRARIAN_SECRET"

# 仓库哨兵：同时存在 `package.json` 与 `apps/api/` 才算仓库根。
# 刻意不用 `.git`（worktree / 源码快照里可能没有）。
_REPO_SENTINELS: Final = (Path("package.json"), Path("apps", "api"))

# 摘要层（#7）的 LLM 配置环境变量；`#11` 之后 `settings` 表里的覆盖优先（由调用方
# 先用 `settings_store.effective_settings()` 叠好再传进来）。
LLM_BASE_URL_ENV: Final = "MC_LIBRARIAN_LLM_BASE_URL"
LLM_MODEL_ENV: Final = "MC_LIBRARIAN_LLM_MODEL"
LLM_API_KEY_ENV: Final = "MC_LIBRARIAN_LLM_API_KEY"

# 单价环境变量（每百万 token 的美元价）。未配置时 `cost_usd` 记 0：只记 token、不计成本，
# 不编一个假单价。
LLM_INPUT_PRICE_ENV: Final = "MC_LIBRARIAN_LLM_INPUT_PRICE_PER_M"
LLM_OUTPUT_PRICE_ENV: Final = "MC_LIBRARIAN_LLM_OUTPUT_PRICE_PER_M"

# 用量告警（#12）的环境变量。阈值 ≤ 0 表示该指标不告警（默认 0，即默认不告警）。
ALERT_WEBHOOK_URL_ENV: Final = "MC_LIBRARIAN_ALERT_WEBHOOK_URL"
ALERT_DAILY_TOKEN_THRESHOLD_ENV: Final = "MC_LIBRARIAN_ALERT_DAILY_TOKEN_THRESHOLD"
ALERT_DAILY_COST_THRESHOLD_ENV: Final = "MC_LIBRARIAN_ALERT_DAILY_COST_THRESHOLD_USD"


def find_repo_root(start: Path | None = None) -> Path | None:
    """从 `start`（默认本文件）向上查找仓库根，找不到返回 None。"""
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if all((candidate / sentinel).exists() for sentinel in _REPO_SENTINELS):
            return candidate
    return None


def resolve_data_dir(value: Path, root: Path | None = None) -> Path:
    """规范化数据根：相对路径相对**仓库根**解析，且结果不允许落在仓库内。

    dev 时进程 cwd 是 `apps/api`，直接按 cwd 解析相对路径会静默落进仓库，所以统一锚到仓库根，
    并对「落进仓库」显式报错（D9：数据根独立、不入库）。
    """
    root = root if root is not None else find_repo_root()
    expanded = value.expanduser()

    if not expanded.is_absolute():
        expanded = (root or Path.cwd()) / expanded

    resolved = expanded.resolve()

    if root is not None and resolved.is_relative_to(root):
        raise ValueError(
            f"数据根不能落在仓库内：{resolved}（仓库根 {root}）。"
            f" 相对路径按仓库根解析，请用例如 `../{DATA_DIR_NAME}`，"
            f" 或用 {DATA_DIR_ENV} 指向仓库之外的绝对路径。"
        )

    return resolved


def default_data_dir(start: Path | None = None) -> Path:
    """数据根的默认值，永远在仓库之外。

    仓库哨兵可见时用 `<repo 的父目录>/mc-librarian-data`；
    否则（例如装在 site-packages 里运行）退回 `~/mc-librarian-data`。
    """
    root = find_repo_root(start)
    if root is not None:
        return root.parent / DATA_DIR_NAME
    return Path.home() / DATA_DIR_NAME


class Settings(BaseSettings):
    """进程级配置，全部可通过 `MC_LIBRARIAN_*` 环境变量覆盖。"""

    model_config = SettingsConfigDict(env_prefix="MC_LIBRARIAN_", extra="ignore")

    data_dir: Path = Field(default_factory=default_data_dir)
    host: str = "127.0.0.1"
    port: int = 8787

    # `MC_LIBRARIAN_ENV`：dev / production。只有 production 会启用 cookie 硬性护栏。
    env: Literal["dev", "production"] = "dev"
    # 会话 cookie 的 `Secure` 属性：本地 http 下给 true 会让浏览器直接丢弃 cookie，
    # 所以默认 false（dev）；生产在 HTTPS 边界后面部署时设 `MC_LIBRARIAN_COOKIE_SECURE=true`。
    cookie_secure: bool = False
    # 主密钥（`MC_LIBRARIAN_SECRET`）：`settings` 表里 `llm.api_key` / `alerts.webhook_url`
    # 的 AES-GCM 主密钥。**必填**——缺失或短于 `secretbox.MIN_SECRET_LENGTH` 时
    # `main.lifespan` 直接抛错，服务起不来（见 `secretbox.require_secret`）。
    secret: str | None = None
    # 会话有效期（小时），与 `settings.security.session_ttl_hours` 的默认值保持一致。
    session_ttl_hours: int = Field(default=168, ge=1, le=24 * 365)
    # 一次性邀请链接的有效期（小时），与 `settings.security.invite_ttl_hours` 一致。
    invite_ttl_hours: int = Field(default=48, ge=1, le=24 * 365)

    # 下载 / 反编译工具链（D24：镜像内只要 JRE + Vineflower + SpecialSource，不装 Gradle）。
    # 两个 jar 路径默认空；只有真正跑 download/decompile 阶段才要求配置，parse-only 任务不受影响。
    java: str = "java"
    vineflower_jar: Path | None = None
    specialsource_jar: Path | None = None
    decompile_memory: str = "4G"

    # 中文摘要（#7）：LLM 配置默认只从环境变量读，`#11` 落地后 `settings` 表里的覆盖优先
    # （读取收敛在 `resolve_llm_config` 一个函数里，覆盖由调用方先叠好）。未配置时
    # `summarize` 阶段明确失败，不静默跳过。
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None
    # 采样温度（契约 `llm.temperature`）：0 表示确定性输出，摘要与问答都要求可复现。
    llm_temperature: float = Field(default=0.0, ge=0, le=2)
    # 单次 LLM 请求的超时（秒）。
    llm_timeout_s: float = Field(default=120.0, gt=0)
    # 摘要按 package 分批，每批类数（规格 §3 建议 30–50，默认 40）。
    summary_batch_size: int = Field(default=40, ge=1, le=500)
    # 摘要生成的并发批次上限（规格 §3 定死 4，用 asyncio 信号量限制）。
    summary_concurrency: int = Field(default=4, ge=1, le=64)

    # 问答（#8）：服务端「检索 → 读取 → 作答」循环。
    # `MC_LIBRARIAN_ASK_ENABLED=false` 时 `/v1/ask` 返回 429（异常时快速止血）。
    ask_enabled: bool = True
    # 单次问答的总超时（秒）：超时走降级，不冒泡成 500。
    ask_timeout_s: int = Field(default=120, ge=1, le=3600)
    # `AskRequest.max_steps` / `max_answer_tokens` 省略时的服务端默认值（契约 `settings.ask.*`）。
    ask_max_steps: int = Field(default=6, ge=1, le=12)
    ask_max_answer_tokens: int = Field(default=8000, ge=500, le=32000)
    # 工具结果累积的上下文预算（token）：超出时从最旧的工具结果开始丢弃（规格 §3）。
    ask_max_context_tokens: int = Field(default=60000, ge=1000)
    # 同时在跑的问答数上限（`settings.security.max_concurrent_asks`）：超出返回 429。
    max_concurrent_asks: int = Field(default=4, ge=1, le=64)

    # 单价（每百万 token 的美元价），只用于 `usage.cost_usd`；未配置时按 0 计。
    llm_input_price_per_m: float | None = None
    llm_output_price_per_m: float | None = None

    # 用量告警（#12）：超阈值推一条 Webhook，**不设自动预算上限**（D23）。
    # 阈值 ≤ 0（默认）表示该指标不告警；未配 Webhook 时也不检查。
    alert_webhook_url: str | None = None
    alert_daily_token_threshold: int = Field(default=0, ge=0)
    alert_daily_cost_threshold_usd: float = Field(default=0.0, ge=0)

    # 索引构建总闸门（`MC_LIBRARIAN_INDEX_ENABLED`）：关掉后 `POST /v1/admin/jobs` 返回 403。
    # 完整五段（下载 + 反编译 + 全树解析）跑在服务进程内，反编译一个版本要 3–4 GiB；
    # 生产节点内存不够，所以在节点上设 false，索引在本地工作站产出后用
    # `POST /v1/admin/versions` 注册。默认 true，保证本地开箱可用。
    # 契约里 `settings.index.enabled` 标成只读：它是部署级配置，只能改环境变量。
    index_enabled: bool = True

    # 用量告警（`#12` 读它）：超阈值只推 Webhook，不自动暂停服务。未配置的阈值保持 None
    # （「没配」和「配成 0」是两回事），由 `resolve_alerts_config` 整理后交给 #12。
    alerts_webhook_url: str | None = None
    alerts_daily_token_threshold: int | None = Field(default=None, ge=1)
    alerts_daily_cost_threshold_usd: float | None = Field(default=None, gt=0)

    @field_validator("decompile_memory")
    @classmethod
    def _check_decompile_memory(cls, value: str) -> str:
        if not re.match(JAVA_XMX_PATTERN, value):
            raise ValueError(f"decompile_memory 必须是形如 4G / 512M 的值，收到：{value}")

        return value

    @field_validator("data_dir")
    @classmethod
    def _resolve(cls, value: Path) -> Path:
        return resolve_data_dir(value)

    @model_validator(mode="after")
    def _guard_production(self) -> Settings:
        """生产护栏：公网部署忘了开 `Secure`，会话 cookie 会明文跑在 HTTP 上。

        dev（默认）下不动，因为本地是 http，`Secure` 会让浏览器直接丢弃 cookie。
        """
        if self.env == ENV_PRODUCTION and not self.cookie_secure:
            raise ValueError(
                "生产环境必须给会话 cookie 加 Secure：请设置 MC_LIBRARIAN_COOKIE_SECURE=true。"
                f"（当前 MC_LIBRARIAN_ENV={self.env}、cookie_secure=false；"
                f" 仅本地调试时才把 MC_LIBRARIAN_ENV 设回 {ENV_DEV}）"
            )

        return self

    @property
    def repo_root(self) -> Path | None:
        return find_repo_root()

    @property
    def data_dir_from_env(self) -> bool:
        """数据根是不是由 `MC_LIBRARIAN_DATA_DIR` 显式指定。"""
        return DATA_DIR_ENV in os.environ


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例，便于在路由中注入。"""
    return Settings()


@dataclass(frozen=True)
class LlmConfig:
    """摘要层调用 LLM 需要的最小配置（OpenAI 兼容端点）。"""

    base_url: str
    model: str
    api_key: str
    temperature: float


@dataclass(frozen=True)
class AlertsConfig:
    """用量告警配置（`#12` 读它）：未配置的阈值是 `None`，不是 0。"""

    webhook_url: str | None
    daily_token_threshold: int | None
    daily_cost_threshold_usd: float | None


@dataclass(frozen=True)
class LlmPricing:
    """每百万 token 的美元单价；未配置的那一侧是 0（只记 token、不计成本）。"""

    input_per_m: float
    output_per_m: float


class LlmConfigError(RuntimeError):
    """LLM 未配置；`summarize` 阶段与 `/v1/ask` 把它翻成可操作的失败，不静默跳过。"""


def resolve_llm_config(settings: Settings) -> LlmConfig:
    """摘要层与问答的 LLM 配置入口。

    只从传进来的 `Settings` 读（`MC_LIBRARIAN_LLM_*` 环境变量，或 `#11` 的
    `settings` 表覆盖——覆盖由调用方先用 `settings_store.effective_settings()` 叠好）。
    缺任何一项都抛 `LlmConfigError` 并列出缺哪个——静默跳过会让索引看起来「有摘要」但实际为空。
    """
    values = {
        LLM_BASE_URL_ENV: settings.llm_base_url,
        LLM_MODEL_ENV: settings.llm_model,
        LLM_API_KEY_ENV: settings.llm_api_key,
    }
    missing = [name for name, value in values.items() if not (value or "").strip()]

    if missing:
        raise LlmConfigError(
            "未配置 LLM，请设置 "
            + " / ".join(missing)
            + "（OpenAI 兼容端点；也可在设置页写入，优先于环境变量）"
        )

    return LlmConfig(
        base_url=str(settings.llm_base_url).rstrip("/"),
        model=str(settings.llm_model),
        api_key=str(settings.llm_api_key),
        temperature=settings.llm_temperature,
    )


def resolve_alerts_config(
    settings: Settings, conn: sqlite3.Connection | None = None
) -> AlertsConfig:
    """用量告警配置（`#12` 的入口）：数据库覆盖 + 环境配置兜底。

    给了 `conn`（控制面连接）就连同 `settings` 表里的覆盖一起解析，否则只用传进来的
    `Settings`（环境配置，或已叠好覆盖的 `effective_settings` 结果）——两种调法等价。
    未配置的字段保持 `None`：`#12` 据此判断「这个阈值要不要告警」，不编 0 之类的假值。
    """
    if conn is not None:
        # 局部导入：只有真的要从库里读覆盖时才需要 `settings_store`（它连带 `db`/`audit`），
        # 而 `config` 被 CLI 与大量测试导入，不必为此把控制面存储拉进每次 import。
        from mc_librarian.settings_store import effective_settings, read_overrides

        settings = effective_settings(settings, read_overrides(conn, settings))

    return AlertsConfig(
        webhook_url=_blank_to_none(settings.alerts_webhook_url),
        daily_token_threshold=settings.alerts_daily_token_threshold,
        daily_cost_threshold_usd=settings.alerts_daily_cost_threshold_usd,
    )


def _blank_to_none(value: str | None) -> str | None:
    """空串与「未配置」等价：环境变量设成空串是常见笔误，别让它变成一个假 URL。"""
    return value if value and value.strip() else None


def resolve_llm_pricing(settings: Settings) -> LlmPricing:
    """`usage.cost_usd` 用的单价；未配置的那一侧按 0 计，不编假单价。"""
    return LlmPricing(
        input_per_m=settings.llm_input_price_per_m or 0.0,
        output_per_m=settings.llm_output_price_per_m or 0.0,
    )


def pricing_configured(settings: Settings) -> bool:
    """是否配了**有效**单价（> 0）。未配时 `cost_usd` 恒为 0（只记 token），用量页据此标注口径。"""
    return bool(settings.llm_input_price_per_m or settings.llm_output_price_per_m)


@dataclass(frozen=True)
class AlertsConfig:
    """用量告警配置；阈值 ≤ 0 表示该指标不告警，`webhook_url` 为空表示没有投递目标。"""

    webhook_url: str | None
    daily_token_threshold: int
    daily_cost_threshold_usd: float


def resolve_alerts_config(settings: Settings) -> AlertsConfig:
    """用量告警阈值的**唯一**读取入口（#11 落地后把「数据库覆盖」加进这里，调用点不动）。

    本票只从环境配置读（`MC_LIBRARIAN_ALERT_*`，经 `Settings` 的 `MC_LIBRARIAN_` 前缀映射）。
    空白的 Webhook 地址按「未配置」处理，避免配了个空格就当有投递目标。
    """
    return AlertsConfig(
        webhook_url=(settings.alert_webhook_url or "").strip() or None,
        daily_token_threshold=settings.alert_daily_token_threshold,
        daily_cost_threshold_usd=settings.alert_daily_cost_threshold_usd,
    )
