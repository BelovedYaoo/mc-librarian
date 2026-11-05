"""API 请求 / 响应模型。

一律对齐 `docs/openapi.yaml` 的 `components.schemas`：字段名、required 与语义都不许漂移，
`tests/test_contract.py` 会比对运行时 schema 与契约。时间字段用 `datetime`，
FastAPI 会声明成 `type: string, format: date-time`。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from mc_librarian.problems import Problem

Role = Literal["admin", "user"]
JobKind = Literal["index"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
JobStage = Literal["download", "decompile", "parse", "summarize", "finalize"]
LogLevel = Literal["debug", "info", "warn", "error"]

# 版本号允许的字符（契约 `JobCreate.version` / `RegisterVersionRequest.version` 的 pattern）。
# 版本号会进文件路径（`db/<version>.sqlite`、`cache/<version>/`），带 `/` 会跑出数据根，
# 所以 `mc-librarian index` 的 CLI 也用同一条正则先校验一次。
VERSION_PATTERN: Final = r"^[A-Za-z0-9._-]+$"

# `-Xmx` 的值：数字 + 单位，避免把任意字符串拼进 java 命令行（设置页与 `config.Settings`
# 的校验共用同一条；`config` 从这里 import，避免 schemas → config 的模块级循环）。
JAVA_XMX_PATTERN: Final = r"^\d+[kKmMgG]$"

# 检索/阅读端点的枚举（契约 `SymbolKind` 与 `SymbolDetail`/`Outline` 的 `kind`）。
SymbolKind = Literal["any", "class", "method", "field"]
SymbolDetailKind = Literal["class", "method", "field"]
ClassKind = Literal["class", "interface", "enum", "record", "annotation"]
ReferenceKind = Literal["call", "field", "type", "other"]
MemberKind = Literal["method", "field"]

__all__ = [
    "VERSION_PATTERN",
    "AcceptInviteRequest",
    "AlertsSettings",
    "AlertsSettingsUpdate",
    "AskCitationEvent",
    "AskDeltaEvent",
    "AskDoneEvent",
    "AskErrorEvent",
    "AskEvent",
    "AskReason",
    "AskRequest",
    "AskResponse",
    "AskSettings",
    "AskSettingsUpdate",
    "AskStep",
    "AskStepEvent",
    "AuditEntry",
    "AuditList",
    "Citation",
    "ClassKind",
    "ConversationDetail",
    "ConversationList",
    "ConversationMessage",
    "ConversationRole",
    "ConversationSummary",
    "CreateUserRequest",
    "CreatedUser",
    "FileSlice",
    "Hit",
    "HitType",
    "IndexSettings",
    "IndexSettingsUpdate",
    "InviteStatus",
    "Job",
    "JobCreate",
    "JobKind",
    "JobList",
    "JobLog",
    "JobLogList",
    "JobOptions",
    "JobStage",
    "JobStatus",
    "LlmSettings",
    "LlmSettingsUpdate",
    "LogLevel",
    "LoginRequest",
    "MemberKind",
    "MinecraftVersion",
    "MinecraftVersionList",
    "Outline",
    "OutlineClass",
    "OutlineMethod",
    "PatchUserRequest",
    "Reference",
    "ReferenceKind",
    "ReferenceResponse",
    "RegisterVersionRequest",
    "Role",
    "SearchDetail",
    "SearchRequest",
    "SearchResponse",
    "SecuritySettings",
    "SecuritySettingsUpdate",
    "Settings",
    "SettingsUpdate",
    "SetupRequest",
    "SetupStatus",
    "SymbolDetail",
    "SymbolDetailKind",
    "SymbolKind",
    "SymbolMatches",
    "SymbolMember",
    "TokenCreateRequest",
    "TokenCreated",
    "TokenInfo",
    "TokenList",
    "Usage",
    "User",
    "UserList",
    "VersionInfo",
    "VersionList",
    "WhyChannel",
    "answer_schemas",
]


class User(BaseModel):
    """对外可见的用户信息：**永远不含** `password_hash`。"""

    id: str
    username: str
    role: Role
    enabled: bool
    created_at: datetime | None = None
    created_by: str | None = None
    last_login_at: datetime | None = None


class SetupStatus(BaseModel):
    """契约里的 `SetupStatus`；`required` 是字段名，与 pydantic 的 required 无关。"""

    required: bool


class SetupRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str
    password: str = Field(min_length=8, max_length=128)


class TokenInfo(BaseModel):
    """契约 `components.schemas.TokenInfo`：**不含** `token_hash`，列表与创建响应共用。"""

    id: str
    name: str
    prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class TokenCreateRequest(BaseModel):
    name: str = Field(max_length=64)
    expires_in_days: int = Field(default=365, ge=1, le=3650)


class TokenCreated(BaseModel):
    """`POST /v1/auth/tokens` 的 201：明文 `token` 仅此一次返回。"""

    token: str
    info: TokenInfo


class TokenList(BaseModel):
    """`GET /v1/auth/tokens` 的 200。"""

    tokens: list[TokenInfo]


class CreateUserRequest(BaseModel):
    """`POST /v1/admin/users`：只给用户名与角色，密码由被邀请人自设。"""

    username: str = Field(min_length=3, max_length=32, pattern=r"^[a-zA-Z0-9_.-]+$")
    role: Role = "user"


class CreatedUser(BaseModel):
    """创建用户的结果：用户 + **仅此一次**返回的邀请链接。"""

    user: User
    invite_url: str


class UserList(BaseModel):
    users: list[User]


class PatchUserRequest(BaseModel):
    """`PATCH /v1/admin/users/{userId}`：字段都可选，只改给了的那几个。"""

    role: Role | None = None
    enabled: bool | None = None


class InviteStatus(BaseModel):
    """`GET /v1/invites/{token}`：链接是否仍可用，以及它对应的用户名。"""

    valid: bool
    username: str | None = None


class AcceptInviteRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class AuditEntry(BaseModel):
    """契约 `components.schemas.AuditEntry`；`actor` 是用户名（缺失时退回 id）。

    系统动作（`version.recorded` 等）没有 actor，`actor` 为 `None`——不是字符串 `"None"`。
    """

    id: str
    actor: str | None
    action: str
    target: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    ip: str | None = None


class AuditList(BaseModel):
    entries: list[AuditEntry]


class JobOptions(BaseModel):
    """创建任务时的 `options`；三个字段见 docs/openapi.yaml 的 `JobCreate`。

    `source_dir` 可省略：省略（或不传 `options`）走完整五段；给了 `options` 但 `source_dir`
    为空由接口层返回 422（空值属于笔误）。
    `obfuscated` 声明该版本源码是否经官方映射 remap；省略时由 `jobs.runner.resolve_obfuscated`
    按 checkpoint 与阶段序列推断（parse-only 缺省按「已 remap」记录）。显式传 `null` 与省略不同，
    由接口层回 422（契约里是非 nullable 的 `boolean`）。
    `force` 为 `true` 时强制重建：绕过「同版本已有任务」的 409，并且即使
    `cache/<version>/fetch.json` 记着源码树已反编译完成也重新反编译（见
    `jobs.runner.reuse_source_tree`）；默认 `false` 时源码树已就绪则复用。
    """

    source_dir: str | None = None
    obfuscated: bool | None = None
    force: bool = False


class JobCreate(BaseModel):
    kind: JobKind
    version: str = Field(min_length=1, max_length=64, pattern=VERSION_PATTERN)
    options: JobOptions | None = None


class Job(BaseModel):
    """契约 `components.schemas.Job`：任务状态与阶段进度。"""

    id: str
    kind: JobKind
    status: JobStatus
    version: str | None = None
    stage: JobStage | None = None
    progress: float = 0
    message: str | None = None
    error: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_by: str | None = None


class JobList(BaseModel):
    jobs: list[Job]


class JobLog(BaseModel):
    id: int
    ts: datetime
    level: LogLevel
    message: str


class JobLogList(BaseModel):
    lines: list[JobLog]


class VersionInfo(BaseModel):
    """契约 `components.schemas.VersionInfo`。"""

    version: str
    files: int
    tokens: int
    indexed_at: datetime
    obfuscated: bool | None = None
    has_summaries: bool | None = None
    source_size_bytes: int | None = None
    # 摘要覆盖率（#7 规格 §8）：已生成的摘要条数 / 类总数，从索引库实时读。
    summary_count: int | None = None
    class_count: int | None = None


class VersionList(BaseModel):
    versions: list[VersionInfo]


class MinecraftVersion(BaseModel):
    """`GET /v1/minecraft-versions` 的一项：Mojang 清单里的一个 release 版本。"""

    id: str
    released_at: datetime


class MinecraftVersionList(BaseModel):
    """`GET /v1/minecraft-versions` 的 200：release 版本，按发布时间倒序。"""

    versions: list[MinecraftVersion]


class RegisterVersionRequest(BaseModel):
    version: str = Field(min_length=1, max_length=64, pattern=VERSION_PATTERN)
    obfuscated: bool = True


class LlmSettings(BaseModel):
    """契约 `Settings.llm`：密钥只写不读，响应里只回 `api_key_set` 布尔值。"""

    base_url: str | None = None
    model: str | None = None
    temperature: float
    timeout_s: float
    api_key_set: bool


class AskSettings(BaseModel):
    """契约 `Settings.ask`。`max_steps` / `max_answer_tokens` 是 `AskRequest` 省略时的默认值。"""

    max_steps: int
    max_answer_tokens: int
    max_context_tokens: int
    timeout_s: int
    enabled: bool


class IndexSettings(BaseModel):
    """契约 `Settings.index`。`enabled` 是索引构建闸门，`java_xmx` 是反编译的 `-Xmx`。

    `java` / `vineflower_jar` / `specialsource_jar` 是工具链路径（空字符串 = 回退环境变量）；
    `data_dir` 是数据根绝对路径（只读）：注册索引时把 `db/<版本>.sqlite` 放进这里。
    """

    enabled: bool
    java_xmx: str
    java: str
    vineflower_jar: str = ""
    specialsource_jar: str = ""
    data_dir: str
    summary_batch_size: int
    summary_concurrency: int


class SecuritySettings(BaseModel):
    """契约 `Settings.security`：会话 / 邀请有效期与问答并发上限。"""

    session_ttl_hours: int
    invite_ttl_hours: int
    max_concurrent_asks: int


class AlertsSettings(BaseModel):
    """契约 `Settings.alerts`：Webhook 只写不读，阈值未配置时整字段省略（不回 0）。"""

    webhook_url_set: bool
    daily_token_threshold: int | None = None


class Settings(BaseModel):
    """契约 `components.schemas.Settings`：`GET/PUT /v1/admin/settings` 的响应。

    值 = 数据库覆盖 + 环境配置兜底（见 `settings_store`）。**只返回真实存在的值**：
    `llm.base_url` / `llm.model` / 两个告警阈值未配置时整个字段省略，不拿契约里的示例默认值
    填充——那会让前端以为「模型已配置」。密钥永远不回明文或密文，只回 `*_set`。
    """

    llm: LlmSettings
    ask: AskSettings
    index: IndexSettings
    security: SecuritySettings
    alerts: AlertsSettings


# ----------------------------------------------------------------------
# `PUT /v1/admin/settings` 的请求体：部分更新，只改请求里给出的字段。
#
# 字段类型一律 `X | None`：值给 `null`（或密钥给空字符串）= 清除该覆盖，回退环境配置。
# `extra="forbid"` 让拼错的字段名直接 422，而不是静默什么都不做。
# `index.enabled` 刻意不在表里：契约把它标成只读（部署级闸门，只能改环境变量）。
# ----------------------------------------------------------------------


class LlmSettingsUpdate(BaseModel):
    """契约 `LlmSettingsUpdate`。`api_key` 给空字符串 = 清除密钥。"""

    model_config = ConfigDict(extra="forbid")

    base_url: str | None = Field(default=None, max_length=500)
    model: str | None = Field(default=None, max_length=200)
    api_key: str | None = Field(default=None, max_length=500)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_s: float | None = Field(default=None, gt=0)


class AskSettingsUpdate(BaseModel):
    """契约 `AskSettingsUpdate`。"""

    model_config = ConfigDict(extra="forbid")

    max_steps: int | None = Field(default=None, ge=1, le=12)
    max_answer_tokens: int | None = Field(default=None, ge=500, le=32000)
    max_context_tokens: int | None = Field(default=None, ge=1000)
    timeout_s: int | None = Field(default=None, ge=1, le=3600)
    enabled: bool | None = None


class IndexSettingsUpdate(BaseModel):
    """契约 `IndexSettingsUpdate`。

    `enabled` 是索引构建闸门（可写：节点上关掉后不再构建索引）；`java` / 两个 jar 是工具链
    路径，给空字符串 = 清除覆盖、回退环境变量。
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    java_xmx: str | None = Field(default=None, pattern=JAVA_XMX_PATTERN)
    java: str | None = Field(default=None, max_length=1000)
    vineflower_jar: str | None = Field(default=None, max_length=1000)
    specialsource_jar: str | None = Field(default=None, max_length=1000)
    summary_batch_size: int | None = Field(default=None, ge=1, le=500)
    summary_concurrency: int | None = Field(default=None, ge=1, le=64)


class SecuritySettingsUpdate(BaseModel):
    """契约 `SecuritySettingsUpdate`。"""

    model_config = ConfigDict(extra="forbid")

    session_ttl_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    invite_ttl_hours: int | None = Field(default=None, ge=1, le=24 * 365)
    max_concurrent_asks: int | None = Field(default=None, ge=1, le=64)


class AlertsSettingsUpdate(BaseModel):
    """契约 `AlertsSettingsUpdate`。`webhook_url` 给空字符串 = 清除。

    阈值下限是 0：写 0 = 关闭该指标告警（与 `alerts.check` 的「阈值 ≤ 0 不告警」一致）。
    """

    model_config = ConfigDict(extra="forbid")

    webhook_url: str | None = Field(default=None, max_length=500)
    daily_token_threshold: int | None = Field(
        default=None, ge=0, description="写 0 = 关闭该指标告警"
    )


class SettingsUpdate(BaseModel):
    """契约 `components.schemas.SettingsUpdate`：`PUT /v1/admin/settings` 的请求体。

    部分更新：只有出现在请求体里的字段会被写，未给的保持原值。
    块字段用 `default_factory` 而不是 `| None`：部分更新用「不出现」表达，整块传 `null` 没有语义，
    直接 422 比静默忽略清楚（`model_fields_set` 两种写法都能分辨「给没给」）。
    """

    model_config = ConfigDict(extra="forbid")

    llm: LlmSettingsUpdate = Field(default_factory=LlmSettingsUpdate)
    ask: AskSettingsUpdate = Field(default_factory=AskSettingsUpdate)
    index: IndexSettingsUpdate = Field(default_factory=IndexSettingsUpdate)
    security: SecuritySettingsUpdate = Field(default_factory=SecuritySettingsUpdate)
    alerts: AlertsSettingsUpdate = Field(default_factory=AlertsSettingsUpdate)


class SymbolMember(BaseModel):
    """`SymbolDetail.members` 的一项：类的一个方法或字段。"""

    kind: MemberKind
    name: str
    signature: str | None = None
    lines: list[int] = Field(min_length=2, max_length=2)


class SymbolDetail(BaseModel):
    """契约 `components.schemas.SymbolDetail`；`lines` 是 1-based 闭区间。"""

    fqn: str
    kind: SymbolDetailKind
    signature: str | None = None
    path: str
    lines: list[int] = Field(min_length=2, max_length=2)
    superclass: str | None = None
    interfaces: list[str] = Field(default_factory=list)
    annotations: list[str] = Field(default_factory=list)
    summary: str | None = None
    members: list[SymbolMember] = Field(default_factory=list)


class SymbolMatches(BaseModel):
    """`GET /v1/symbol` 的 200。"""

    matches: list[SymbolDetail]


class Reference(BaseModel):
    """一条引用；`lines` 固定两元素，引用落在同一行时是 `[line, line]`。"""

    path: str
    lines: list[int] = Field(min_length=2, max_length=2)
    enclosing: str | None = None
    kind: ReferenceKind | None = None
    snippet: str | None = None


class ReferenceResponse(BaseModel):
    """契约 `components.schemas.ReferenceResponse`。"""

    symbol: str
    version: str
    references: list[Reference]


class OutlineMethod(BaseModel):
    name: str
    signature: str | None = None
    lines: list[int] = Field(min_length=2, max_length=2)


class OutlineClass(BaseModel):
    fqn: str
    kind: ClassKind
    signature: str | None = None
    lines: list[int] = Field(min_length=2, max_length=2)
    methods: list[OutlineMethod] = Field(default_factory=list)


class Outline(BaseModel):
    """契约 `components.schemas.Outline`；不含方法体与字段列表。

    `package` 可为 null：`data/` 下的文件与无包声明的 `.java` 都没有包名。
    """

    path: str
    package: str | None
    imports: list[str] = Field(default_factory=list)
    classes: list[OutlineClass]


class FileSlice(BaseModel):
    """契约 `components.schemas.FileSlice`；`lines` 语义见规格 §2。"""

    path: str
    version: str
    start: int
    end: int
    total_lines: int | None = None
    content: str
    truncated: bool


# ----------------------------------------------------------------------
# 混合检索（`POST /v1/search`，issue #6）
# ----------------------------------------------------------------------

SearchDetail = Literal["locate", "snippet"]
# `file` 是没有任何符号覆盖的位置（如 `package` 行 / `data/` 下的 JSON）。
HitType = Literal["class", "method", "field", "file"]
# `why` 的取值域与固定顺序（issue #6 规格 §4）；`summary` 需要索引库里有 `summaries_fts`（#7）。
WhyChannel = Literal["summary", "symbol", "literal", "reference"]


class SearchRequest(BaseModel):
    """契约 `components.schemas.SearchRequest`；`kind` 只作用于 symbol 通道。"""

    query: str = Field(min_length=1, max_length=512)
    version: str
    path_prefix: str | None = None
    kind: SymbolKind = "any"
    detail: SearchDetail = "snippet"
    limit: int = Field(default=10, ge=1, le=50)
    max_tokens: int = Field(default=4000, ge=200, le=32000)


class Hit(BaseModel):
    """契约 `components.schemas.Hit`；`detail=locate` 时不带 `snippet` / `summary`。"""

    id: str
    type: HitType
    version: str
    path: str
    symbol: str | None = None
    lines: list[int] = Field(min_length=2, max_length=2)
    score: float
    why: list[WhyChannel]
    summary: str | None = None
    snippet: str | None = None
    tokens: int | None = None


class SearchResponse(BaseModel):
    """契约 `components.schemas.SearchResponse`。

    `total` 是融合前的候选总数（各路候选之和），不因 `limit` / `max_tokens` 截断而变。
    """

    query: str
    version: str
    hits: list[Hit]
    suggestions: list[str] = Field(default_factory=list)
    total: int
    took_ms: int


# ----------------------------------------------------------------------
# 问答（`POST /v1/ask`，issue #8）
# ----------------------------------------------------------------------

# `done` 事件/一次性响应的收尾原因（规格 §2/§4/§5）：
# `stop` = 模型不再调用工具、正常作答；`max_steps` = 达到检索步数上限后强制收尾；
# `degraded` = 模型超时/不可用/返回非法响应，用已召回的检索结果降级作答。
AskReason = Literal["stop", "max_steps", "degraded"]


class Usage(BaseModel):
    """契约 `components.schemas.Usage`：一次问答的用量（含降级时已知的部分）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0


class UsageBucket(BaseModel):
    """契约 `UsageReport.buckets` 的一项：一个分组（天/用户/版本/模型）的合计。"""

    key: str
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageReport(BaseModel):
    """契约 `components.schemas.UsageReport`（`GET /v1/admin/usage`，issue #12）。

    `totals` 是区间合计、`buckets` 是分组明细，两者同源（用量页直接用这里的数字，
    不再在前端算一遍）；`requests` 是区间请求数（顶部卡片的第四个数字）。
    只审计 token，不含金额（`usage.cost_usd` 已在迁移 v11 删除）。

    `from` 是 Python 关键字，字段写成 `from_` + `serialization_alias`：对外的 JSON 与
    OpenAPI 属性名仍是 `from`，构造时用 `from_=`（mypy 也认这个签名）。
    """

    from_: date = Field(serialization_alias="from")
    to: date
    requests: int
    totals: Usage
    buckets: list[UsageBucket]


class Citation(BaseModel):
    """契约 `components.schemas.Citation`：一条代码位置引用。"""

    path: str
    lines: list[int] = Field(min_length=2, max_length=2)
    symbol: str | None = None


class AskStep(BaseModel):
    """契约 `components.schemas.AskStep`：一次性响应里的工具轨迹（调了什么、参数）。"""

    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class AskRequest(BaseModel):
    """契约 `components.schemas.AskRequest`；`version` 必传，模型不可改。

    `max_steps` / `max_answer_tokens` 省略时用服务端设置（`settings.ask.*`）——设置页改的就是
    这两个默认值，见 `api/v1/answer.py`。`conversation_id` 省略时新建会话，给了就续聊该会话。
    """

    question: str = Field(min_length=1, max_length=2000)
    version: str
    conversation_id: str | None = None
    max_steps: int | None = Field(default=None, ge=1, le=12)
    max_answer_tokens: int | None = Field(default=None, ge=500, le=32000)


class AskResponse(BaseModel):
    """契约 `components.schemas.AskResponse`：`Accept: application/json` 时的一次性返回。"""

    answer: str
    citations: list[Citation]
    steps: list[AskStep]
    usage: Usage
    reason: AskReason


# ----------------------------------------------------------------------
# 历史会话（问答页左栏，迁移 v9）
# ----------------------------------------------------------------------

# `messages.role` 的取值域，与建表的 CHECK 一致。
ConversationRole = Literal["user", "assistant"]


class ConversationSummary(BaseModel):
    """`GET /v1/conversations` 的一项：左栏列表要的字段（标题 + 相对时间 + 条数）。"""

    id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int


class ConversationList(BaseModel):
    """`GET /v1/conversations` 的 200；按 `updated_at` 降序。"""

    conversations: list[ConversationSummary]


class ConversationMessage(BaseModel):
    """`ConversationDetail.messages` 的一项。

    `usage` 只有助手消息有（用户消息为 `null`）；`citations` 用户消息为空数组。
    `version` 是这一轮提问用的版本（迁移 v10 之前落库的历史行为 `null`，前端回退到当前选中的
    版本）——引用的 `path` 是版本相关的，历史引用必须按当时版本打开。
    """

    role: ConversationRole
    content: str
    citations: list[Citation]
    usage: Usage | None = None
    version: str | None = None
    created_at: datetime


class ConversationDetail(BaseModel):
    """`GET /v1/conversations/{id}` 的 200：会话标题 + 全部消息。"""

    id: str
    title: str
    messages: list[ConversationMessage]


class AskStepEvent(BaseModel):
    """`step` 事件：工具调用前带 `args`，返回后带 `ok` / `hits`（规格 §4）。"""

    type: Literal["step"]
    tool: str
    args: dict[str, Any] | None = None
    ok: bool | None = None
    hits: int | None = None


class AskDeltaEvent(BaseModel):
    """`delta` 事件：答案增量。"""

    type: Literal["delta"]
    text: str


class AskCitationEvent(BaseModel):
    """`citation` 事件：一条代码位置引用，前端做成可点击。"""

    type: Literal["citation"]
    path: str
    lines: list[int] = Field(min_length=2, max_length=2)
    symbol: str | None = None


class AskDoneEvent(BaseModel):
    """`done` 事件：正常、`max_steps` 或降级收尾，都带上已知的 usage 与所属会话 id。"""

    type: Literal["done"]
    usage: Usage
    reason: AskReason
    conversation_id: str


class AskErrorEvent(BaseModel):
    """`error` 事件：流开始后不可恢复的错误（HTTP 层已经无法再改状态码）。"""

    type: Literal["error"]
    problem: Problem


# SSE 的 5 种事件（契约 `AskEvent` 的 oneOf，按 `type` 区分）。
AskEvent = Annotated[
    AskStepEvent | AskDeltaEvent | AskCitationEvent | AskDoneEvent | AskErrorEvent,
    Field(discriminator="type"),
]


def answer_schemas() -> dict[str, dict[str, Any]]:
    """`/v1/ask` 的 200 响应 schema（含嵌套模型），供 `main._build_openapi` 合并。

    这条响应有两个 content type（`text/event-stream` 与 `application/json`），而 FastAPI 的
    `response_model` 只能挂一个，所以 200 只能写成裸 `$ref`；裸 `$ref` 不会自动进
    `components.schemas`（同 `Problem` 的处境），这里显式生成。`$defs` 里的嵌套模型一并展开，
    否则 `$ref` 指向不存在的组件，运行时 spec 过不了 `openapi-spec-validator`。
    """
    schemas: dict[str, dict[str, Any]] = {}

    for name, adapter in (
        ("AskEvent", TypeAdapter(AskEvent)),
        ("AskResponse", TypeAdapter(AskResponse)),
    ):
        schema = adapter.json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(schema.pop("$defs", {}))
        schemas[name] = schema

    return schemas
