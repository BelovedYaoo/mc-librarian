"""`settings` 表的读写与「数据库覆盖 + 环境配置兜底」（issue #11）。

**覆盖而不是替换**：库里有值的键用库里的，没有的键退回 `MC_LIBRARIAN_*` 环境配置
（`GET /v1/admin/settings` 的语义）。所以这里只存**被显式改过**的键——`DELETE` 一行就等于
「回退环境配置」，不需要写一个「空值」占位。

**键名就是契约 `Settings` 里的路径**（`llm.base_url` 等），`SettingSpec` 把它映射到
`config.Settings` 的字段名；`effective_settings()` 拿这个映射把覆盖叠到运行时配置上。
字段名映射集中在 `BLOCK_SPECS` 一处，避免各处手写字符串漂移。

**实时读**：每次请求都新读一次库（`read_overrides`），不做进程级缓存——「保存后立即生效」
（验收第 1 条）靠的就是这个；`settings` 表只有十几行，读一次比缓存失效逻辑便宜。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from mc_librarian import audit
from mc_librarian.db import utc_now
from mc_librarian.schemas import SettingsUpdate
from mc_librarian.secretbox import decrypt, encrypt, require_secret

if TYPE_CHECKING:  # 只为类型标注：`config` 在运行时会 import 本模块（`resolve_alerts_config`）。
    from collections.abc import Mapping

    from mc_librarian.config import Settings

# 契约键：与 docs/openapi.yaml 的 `Settings` 路径一一对应。
KEY_LLM_BASE_URL: Final = "llm.base_url"
KEY_LLM_MODEL: Final = "llm.model"
KEY_LLM_API_KEY: Final = "llm.api_key"
KEY_LLM_TEMPERATURE: Final = "llm.temperature"
KEY_LLM_TIMEOUT_S: Final = "llm.timeout_s"
KEY_ASK_MAX_STEPS: Final = "ask.max_steps"
KEY_ASK_MAX_ANSWER_TOKENS: Final = "ask.max_answer_tokens"
KEY_ASK_MAX_CONTEXT_TOKENS: Final = "ask.max_context_tokens"
KEY_ASK_TIMEOUT_S: Final = "ask.timeout_s"
KEY_ASK_ENABLED: Final = "ask.enabled"
KEY_INDEX_ENABLED: Final = "index.enabled"
KEY_INDEX_JAVA_XMX: Final = "index.java_xmx"
KEY_INDEX_JAVA: Final = "index.java"
KEY_INDEX_VINEFLOWER_JAR: Final = "index.vineflower_jar"
KEY_INDEX_SPECIALSOURCE_JAR: Final = "index.specialsource_jar"
KEY_INDEX_SUMMARY_BATCH_SIZE: Final = "index.summary_batch_size"
KEY_INDEX_SUMMARY_CONCURRENCY: Final = "index.summary_concurrency"
KEY_SECURITY_SESSION_TTL_HOURS: Final = "security.session_ttl_hours"
KEY_SECURITY_INVITE_TTL_HOURS: Final = "security.invite_ttl_hours"
KEY_SECURITY_MAX_CONCURRENT_ASKS: Final = "security.max_concurrent_asks"
KEY_ALERTS_WEBHOOK_URL: Final = "alerts.webhook_url"
KEY_ALERTS_DAILY_TOKEN_THRESHOLD: Final = "alerts.daily_token_threshold"


@dataclass(frozen=True)
class SettingSpec:
    """一条可覆盖的设置：契约键 → `config.Settings` 字段名（`encrypted` 的走 AES-GCM）。"""

    key: str
    field: str
    encrypted: bool = False


# 按契约的块分组：键是请求体里的块名，值是「块内字段名 → spec」。
#
# 刻意**不**放进表里的环境变量（只能改环境变量，代码里也说明理由）：
# - `data_dir` / `host` / `port` / `env`：启动参数——改了要换地方监听或换数据根，
#   运行时改不了已打开的库与已绑定的端口；
# - `cookie_secure`：安全护栏，放进 DB 等于护栏可被关掉；
# - `secret`：解密 `settings` 表的主密钥，放进它自己加密的表里是循环依赖。
BLOCK_SPECS: Final[dict[str, dict[str, SettingSpec]]] = {
    "llm": {
        "base_url": SettingSpec(KEY_LLM_BASE_URL, "llm_base_url"),
        "model": SettingSpec(KEY_LLM_MODEL, "llm_model"),
        "api_key": SettingSpec(KEY_LLM_API_KEY, "llm_api_key", encrypted=True),
        "temperature": SettingSpec(KEY_LLM_TEMPERATURE, "llm_temperature"),
        "timeout_s": SettingSpec(KEY_LLM_TIMEOUT_S, "llm_timeout_s"),
    },
    "ask": {
        "max_steps": SettingSpec(KEY_ASK_MAX_STEPS, "ask_max_steps"),
        "max_answer_tokens": SettingSpec(KEY_ASK_MAX_ANSWER_TOKENS, "ask_max_answer_tokens"),
        "max_context_tokens": SettingSpec(KEY_ASK_MAX_CONTEXT_TOKENS, "ask_max_context_tokens"),
        "timeout_s": SettingSpec(KEY_ASK_TIMEOUT_S, "ask_timeout_s"),
        "enabled": SettingSpec(KEY_ASK_ENABLED, "ask_enabled"),
    },
    "index": {
        "enabled": SettingSpec(KEY_INDEX_ENABLED, "index_enabled"),
        "java_xmx": SettingSpec(KEY_INDEX_JAVA_XMX, "decompile_memory"),
        "java": SettingSpec(KEY_INDEX_JAVA, "java"),
        "vineflower_jar": SettingSpec(KEY_INDEX_VINEFLOWER_JAR, "vineflower_jar"),
        "specialsource_jar": SettingSpec(KEY_INDEX_SPECIALSOURCE_JAR, "specialsource_jar"),
        "summary_batch_size": SettingSpec(
            KEY_INDEX_SUMMARY_BATCH_SIZE, "summary_batch_size"
        ),
        "summary_concurrency": SettingSpec(
            KEY_INDEX_SUMMARY_CONCURRENCY, "summary_concurrency"
        ),
    },
    "security": {
        "session_ttl_hours": SettingSpec(
            KEY_SECURITY_SESSION_TTL_HOURS, "session_ttl_hours"
        ),
        "invite_ttl_hours": SettingSpec(KEY_SECURITY_INVITE_TTL_HOURS, "invite_ttl_hours"),
        "max_concurrent_asks": SettingSpec(
            KEY_SECURITY_MAX_CONCURRENT_ASKS, "max_concurrent_asks"
        ),
    },
    "alerts": {
        "webhook_url": SettingSpec(KEY_ALERTS_WEBHOOK_URL, "alerts_webhook_url", encrypted=True),
        "daily_token_threshold": SettingSpec(
            KEY_ALERTS_DAILY_TOKEN_THRESHOLD, "alerts_daily_token_threshold"
        ),
    },
}

# 契约键 → spec（读库时按 `settings.key` 反查）。
SPECS_BY_KEY: Final[dict[str, SettingSpec]] = {
    spec.key: spec for specs in BLOCK_SPECS.values() for spec in specs.values()
}


def read_overrides(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    """读出全部覆盖项，键是 **`Settings` 的字段名**（直接喂给 `effective_settings`）。

    加密行（`encrypted=1`）在这里解密；解不开抛 `SecretError`——静默当成「没配」会让
    一个已配置的密钥凭空消失，比明确报错难查得多。
    """
    overrides: dict[str, Any] = {}

    for row in conn.execute("SELECT key, value_json, encrypted FROM settings"):
        spec = SPECS_BY_KEY.get(str(row["key"]))

        if spec is None:
            # 库里可能有旧版本写入、当前已不再支持的键：跳过，不影响其它设置生效。
            continue

        raw = str(row["value_json"])
        overrides[spec.field] = (
            decrypt(require_secret(settings.secret), raw) if row["encrypted"] else json.loads(raw)
        )

    return overrides


def effective_settings(settings: Settings, overrides: Mapping[str, Any]) -> Settings:
    """数据库覆盖 + 环境配置兜底。

    走一次完整的 pydantic 校验而不是 `model_copy`：库里被手工改坏的值（类型不对、超出取值域）
    在读取时就变成明确的 `ValidationError`，不会带着坏值一路跑到 java 命令行或 LLM 请求里。
    """
    if not overrides:
        return settings

    return type(settings).model_validate({**settings.model_dump(), **overrides})


def apply_update(
    conn: sqlite3.Connection,
    payload: SettingsUpdate,
    *,
    settings: Settings,
    actor_id: str | None,
    ip: str | None = None,
) -> list[str]:
    """部分更新：只处理请求里**给出**的字段，返回真正改动过的契约键（审计只记键，不记值）。

    值给 `null` 或空字符串 = 清除该覆盖（回退环境配置 / 密钥变成「未设置」）。
    调用方负责把它包在 `transaction()` 里（与审计同事务）。
    """
    changed: list[str] = []

    for block_name in payload.model_fields_set:
        block = getattr(payload, block_name)

        for field_name in block.model_fields_set:
            spec = BLOCK_SPECS[block_name][field_name]

            if _write(
                conn, spec, getattr(block, field_name), settings=settings, actor_id=actor_id
            ):
                changed.append(spec.key)

    audit.record(
        conn,
        actor_id=actor_id,
        action=audit.ACTION_SETTINGS_UPDATED,
        # 只记「改了哪些键」：值里可能有密钥，审计页与日志都不该出现明文。
        detail={"keys": changed},
        ip=ip,
    )

    return changed


def _write(
    conn: sqlite3.Connection,
    spec: SettingSpec,
    value: Any,
    *,
    settings: Settings,
    actor_id: str | None,
) -> bool:
    """写（或删）一行覆盖；返回是否真的改了库。

    「没改」的判定：非加密键比较存储文本；加密键每次都是新的 nonce，密文必然不同，
    因此只要给了值就算改动（`encrypted` 行不做明文比较，也不该为此多解一次密）。
    """
    if value is None or (isinstance(value, str) and value.strip() == ""):
        cursor = conn.execute("DELETE FROM settings WHERE key = ?", (spec.key,))

        return cursor.rowcount > 0

    stored = (
        encrypt(require_secret(settings.secret), str(value))
        if spec.encrypted
        else json.dumps(value, ensure_ascii=False)
    )

    if not spec.encrypted:
        row = conn.execute(
            "SELECT value_json FROM settings WHERE key = ?", (spec.key,)
        ).fetchone()

        if row is not None and str(row["value_json"]) == stored:
            return False

    conn.execute(
        """
        INSERT INTO settings (key, value_json, encrypted, updated_at, updated_by)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (key) DO UPDATE SET
          value_json = excluded.value_json,
          encrypted  = excluded.encrypted,
          updated_at = excluded.updated_at,
          updated_by = excluded.updated_by
        """,
        (spec.key, stored, 1 if spec.encrypted else 0, utc_now(), actor_id),
    )

    return True
