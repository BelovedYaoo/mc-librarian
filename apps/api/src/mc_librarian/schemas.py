"""API 请求 / 响应模型。

一律对齐 `docs/openapi.yaml` 的 `components.schemas`：字段名、required 与语义都不许漂移，
`tests/test_contract.py` 会比对运行时 schema 与契约。时间字段用 `datetime`，
FastAPI 会声明成 `type: string, format: date-time`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, Field

Role = Literal["admin", "user"]
JobKind = Literal["index"]
JobStatus = Literal["queued", "running", "succeeded", "failed", "canceled"]
JobStage = Literal["download", "decompile", "parse", "summarize", "finalize"]
LogLevel = Literal["debug", "info", "warn", "error"]

# 版本号允许的字符（契约 `JobCreate.version` / `RegisterVersionRequest.version` 的 pattern）。
# 版本号会进文件路径（`db/<version>.sqlite`、`cache/<version>/`），带 `/` 会跑出数据根，
# 所以 `mc-librarian index` 的 CLI 也用同一条正则先校验一次。
VERSION_PATTERN: Final = r"^[A-Za-z0-9._-]+$"

# 检索/阅读端点的枚举（契约 `SymbolKind` 与 `SymbolDetail`/`Outline` 的 `kind`）。
SymbolKind = Literal["any", "class", "method", "field"]
SymbolDetailKind = Literal["class", "method", "field"]
ClassKind = Literal["class", "interface", "enum", "record", "annotation"]
ReferenceKind = Literal["call", "field", "type", "other"]
MemberKind = Literal["method", "field"]

__all__ = [
    "VERSION_PATTERN",
    "AcceptInviteRequest",
    "AuditEntry",
    "AuditList",
    "ClassKind",
    "CreateUserRequest",
    "CreatedUser",
    "FileSlice",
    "Hit",
    "HitType",
    "IndexSettings",
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
    "LogLevel",
    "LoginRequest",
    "MemberKind",
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
    "Settings",
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
    "User",
    "UserList",
    "VersionInfo",
    "VersionList",
    "WhyChannel",
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


class VersionList(BaseModel):
    versions: list[VersionInfo]


class RegisterVersionRequest(BaseModel):
    version: str = Field(min_length=1, max_length=64, pattern=VERSION_PATTERN)
    obfuscated: bool = True


class IndexSettings(BaseModel):
    """契约 `Settings.index` 里当前**真正有实现**的两个字段（`GET /v1/admin/settings`）。

    `summary_batch_size`/`summary_concurrency` 是 #7 的摘要参数，尚无实现，刻意不返回。
    """

    enabled: bool
    java_xmx: str


class Settings(BaseModel):
    """契约 `components.schemas.Settings`。

    当前只有 `index` 块有实现（值从运行时配置派生）；`llm`/`ask`/`security`/`alerts` 尚未实现，
    整块省略而不是拿契约里的示例默认值填充——那会让前端以为「模型已配置、密钥已设置」。
    """

    index: IndexSettings


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
# `why` 的取值域与固定顺序（issue #6 规格 §4）；`summary` 由 #7 落地后才可能出现。
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
