"""控制面 SQLite（`<data_dir>/control.sqlite`）：连接管理与最小迁移机制。

设计约束（见 `docs/00-overview.md` §5.1）：

- 只用标准库 `sqlite3`，不引入 ORM；
- 迁移是「版本表 + 一组语句」：每个迁移是一个版本号加一组 `CREATE ... IF NOT EXISTS`
  （需要改 CHECK 时按 SQLite 的标准流程重建表，见 v5），启动时把 `schema_version` 里
  没有的版本按序执行一遍并记账，因此重复启动是安全的；
- 后续 ticket 加表只需在 `MIGRATIONS` 末尾追加一条，不动已有版本。

时间统一存 UTC 的定宽文本（`YYYY-MM-DDTHH:MM:SSZ`），这样 SQL 里可以直接按字符串比较先后。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

CONTROL_DB_NAME: Final = "control.sqlite"

# 定宽、带 Z 的 UTC 时间戳：`expires_at > now` 之类的比较可以按字符串做。
TIMESTAMP_FORMAT: Final = "%Y-%m-%dT%H:%M:%SZ"


def utc_now() -> str:
    """当前 UTC 时间（定宽文本）。"""
    return datetime.now(UTC).strftime(TIMESTAMP_FORMAT)


def utc_after(hours: float) -> str:
    """`hours` 小时之后的 UTC 时间（定宽文本）。"""
    return (datetime.now(UTC) + timedelta(hours=hours)).strftime(TIMESTAMP_FORMAT)


@dataclass(frozen=True)
class Migration:
    """一次 schema 迁移：版本号 + 一组幂等语句。

    `foreign_keys_off` 用于需要重建表的迁移（SQLite 改不了 CHECK）：删旧表时外键若开着，
    `ON DELETE CASCADE` 会把子表数据一起清掉（`job_logs` 引用 `jobs(id)`），所以重建期间
    必须先关外键，迁移跑完再打开。
    """

    version: int
    statements: tuple[str, ...]
    foreign_keys_off: bool = False


# v1：账号与会话；v2：个人访问令牌（pats）；v3：邀请与审计（invites/audit）；
# v4：索引任务框架（jobs/job_logs/versions）；v5：`jobs.stage` 扩出 download/decompile；
# v6：用量（usage，issue #8 建表、`#12` 只读聚合）；
# v7：告警去重（alert_events，issue #12）；v8：实例设置（settings，issue #11）；
# v9：历史会话（conversations/messages，问答页左栏）；
# v10：`messages.version`（历史引用的版本口径）。
# v11：删 `usage.cost_usd`（用户决定只审计 token、不算金额）。
# 后续 ticket 在末尾继续追加，不改已有版本。
MIGRATIONS: Final[tuple[Migration, ...]] = (
    Migration(
        version=1,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS users (
              id            TEXT PRIMARY KEY,
              username      TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,
              role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
              enabled       INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
              created_at    TEXT NOT NULL,
              created_by    TEXT REFERENCES users (id),
              last_login_at TEXT
            )
            """,
            # 会话表只存 token 的 sha256，明文 token 只出现在 cookie 里。
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id         TEXT PRIMARY KEY,
              user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
              token_hash TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              ip         TEXT,
              user_agent TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions (expires_at)",
        ),
    ),
    Migration(
        version=2,
        statements=(
            # 只存明文 token 的 sha256；prefix 是明文前 8 位（辨认用，无法还原）。
            """
            CREATE TABLE IF NOT EXISTS pats (
              id           TEXT PRIMARY KEY,
              user_id      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
              name         TEXT NOT NULL,
              token_hash   TEXT NOT NULL UNIQUE,
              prefix       TEXT NOT NULL,
              created_at   TEXT NOT NULL,
              expires_at   TEXT NOT NULL,
              last_used_at TEXT,
              revoked_at   TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_pats_user_id ON pats (user_id)",
        ),
    ),
    # v3：用户管理所需的邀请与审计（issue #9）。
    Migration(
        version=3,
        statements=(
            # 一次性邀请：明文 token 只在创建时返回一次，库里只存 sha256（同 sessions）。
            """
            CREATE TABLE IF NOT EXISTS invites (
              id         TEXT PRIMARY KEY,
              user_id    TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
              token_hash TEXT NOT NULL UNIQUE,
              created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL,
              used_at    TEXT
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_invites_user_id ON invites (user_id)",
            "CREATE INDEX IF NOT EXISTS idx_invites_token_hash ON invites (token_hash)",
            # 审计：actor 可为空（系统动作），detail 是 JSON 文本。
            """
            CREATE TABLE IF NOT EXISTS audit (
              id          TEXT PRIMARY KEY,
              actor_id    TEXT REFERENCES users (id),
              action      TEXT NOT NULL,
              target      TEXT,
              detail_json TEXT,
              ip          TEXT,
              created_at  TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_audit_created_at ON audit (created_at)",
            "CREATE INDEX IF NOT EXISTS idx_audit_actor_id ON audit (actor_id)",
        ),
    ),
    # v4：索引任务框架（issue #4）。——任务、任务日志、版本注册表。
    # `jobs` 里的 `checkpoint`/`options` 存 JSON 文本：`checkpoint` 记已完成的阶段与产物，
    # 供失败后从下一阶段续跑；`options` 记 `source_dir` 等创建参数。
    # `kind` 与 `stage` 的 CHECK 就是接口层的取值域：本票只支持 `index` 与
    # `parse`/`summarize`/`finalize` 三段阶段（`stage` 允许 NULL：任务尚未进入任何阶段）。
    # 这里直接改 v4 而不是补一条 v5：`jobs` 由本票引入、还没有部署，没有历史行要容纳。
    # `#21` 引入 `download`/`decompile` 时库已经部署过，那时要**新增**一条迁移，不能再回头改 v4。
    # 存储层与接口层同域：`row_to_job` 的严格投影就不会读到域外的值而 500。
    Migration(
        version=4,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id               TEXT PRIMARY KEY,
              kind             TEXT NOT NULL CHECK (kind IN ('index')),
              version          TEXT NOT NULL,
              status           TEXT NOT NULL CHECK (
                                 status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')
                               ),
              stage            TEXT CHECK (stage IS NULL OR stage IN (
                                 'parse', 'summarize', 'finalize'
                               )),
              progress         REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
              message          TEXT,
              error            TEXT,
              checkpoint       TEXT NOT NULL DEFAULT '{}',
              options          TEXT NOT NULL DEFAULT '{}',
              cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
              created_at       TEXT NOT NULL,
              started_at       TEXT,
              finished_at      TEXT,
              created_by       TEXT REFERENCES users (id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_version ON jobs (version, status)",
            # 日志 id 单调递增，`?after=<id>` 才能稳定增量拉取。
            """
            CREATE TABLE IF NOT EXISTS job_logs (
              id      INTEGER PRIMARY KEY AUTOINCREMENT,
              job_id  TEXT NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
              ts      TEXT NOT NULL,
              level   TEXT NOT NULL CHECK (level IN ('debug', 'info', 'warn', 'error')),
              message TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_job_logs_job ON job_logs (job_id, id)",
            # 版本注册表：`db_path` 指向数据根里的 `<version>.sqlite`（job 产出的可能为空）。
            """
            CREATE TABLE IF NOT EXISTS versions (
              version           TEXT PRIMARY KEY,
              files             INTEGER NOT NULL DEFAULT 0,
              tokens            INTEGER NOT NULL DEFAULT 0,
              indexed_at        TEXT NOT NULL,
              obfuscated        INTEGER NOT NULL DEFAULT 1 CHECK (obfuscated IN (0, 1)),
              has_summaries     INTEGER NOT NULL DEFAULT 0 CHECK (has_summaries IN (0, 1)),
              source_size_bytes INTEGER NOT NULL DEFAULT 0,
              db_path           TEXT
            )
            """,
        ),
    ),
    # v5：`jobs.stage` 的取值域扩出 `download`/`decompile`（issue #21）。
    # SQLite 不能直接改 CHECK，只能按标准流程重建表：建新表 → 拷数据 → 删旧表 → 改名 → 重建索引。
    # `job_logs` 通过外键引用 `jobs(id)` 且带 `ON DELETE CASCADE`，删 `jobs` 时外键若开着会
    # 连带清空 `job_logs`，所以本迁移要求 `migrate()` 在重建期间关闭外键（`foreign_keys_off`）。
    # 必须新增迁移而不是回头改 v4：#4 的注释已写明，v4 落地时库还没部署，而本票落地时库
    # 可能已经部署过，只能追加。
    Migration(
        version=5,
        foreign_keys_off=True,
        statements=(
            """
            CREATE TABLE jobs_v5 (
              id               TEXT PRIMARY KEY,
              kind             TEXT NOT NULL CHECK (kind IN ('index')),
              version          TEXT NOT NULL,
              status           TEXT NOT NULL CHECK (
                                 status IN ('queued', 'running', 'succeeded', 'failed', 'canceled')
                               ),
              stage            TEXT CHECK (stage IS NULL OR stage IN (
                                 'download', 'decompile', 'parse', 'summarize', 'finalize'
                               )),
              progress         REAL NOT NULL DEFAULT 0 CHECK (progress >= 0 AND progress <= 1),
              message          TEXT,
              error            TEXT,
              checkpoint       TEXT NOT NULL DEFAULT '{}',
              options          TEXT NOT NULL DEFAULT '{}',
              cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0, 1)),
              created_at       TEXT NOT NULL,
              started_at       TEXT,
              finished_at      TEXT,
              created_by       TEXT REFERENCES users (id)
            )
            """,
            """
            INSERT INTO jobs_v5 (
              id, kind, version, status, stage, progress, message, error,
              checkpoint, options, cancel_requested, created_at, started_at, finished_at, created_by
            )
            SELECT
              id, kind, version, status, stage, progress, message, error,
              checkpoint, options, cancel_requested, created_at, started_at, finished_at, created_by
            FROM jobs
            """,
            "DROP TABLE jobs",
            "ALTER TABLE jobs_v5 RENAME TO jobs",
            "CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs (status, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_jobs_version ON jobs (version, status)",
        ),
    ),
    # v6：用量（issue #8）。每次 `/v1/ask` 写一行（含降级的情况），`#12` 只做聚合与告警。
    # 只记 token（`cost_usd` 已在 v11 删除）。
    Migration(
        version=6,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS usage (
              id                INTEGER PRIMARY KEY,
              user_id           TEXT REFERENCES users (id),
              kind              TEXT NOT NULL,
              model             TEXT,
              prompt_tokens     INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              cost_usd          REAL NOT NULL DEFAULT 0,
              latency_ms        INTEGER NOT NULL DEFAULT 0,
              version           TEXT,
              created_at        TEXT NOT NULL
            )
            """,
        ),
    ),
    # v7：用量告警的去重账（issue #12）。`UNIQUE(day, metric)` 就是「同日同指标只告警一次」的
    # 实现方式：插入冲突即已告警过（`INSERT ... ON CONFLICT DO NOTHING` + `rowcount` 判定），
    # 不需要先查后写。`day` 是本地日期（UTC+8），与用量页的分天口径一致。
    #
    # v8：实例设置（issue #11）。只存**被显式改过**的键（`settings_store`）：删一行 = 回退
    # 环境配置，所以「数据库覆盖 + 环境配置兜底」不需要空值占位。
    # `key` 就是契约 `Settings` 里的路径（`llm.base_url` 等）；`value_json` 是 JSON 文本，
    # `encrypted=1` 的行存 AES-GCM 密文（base64，主密钥来自 `MC_LIBRARIAN_SECRET`，
    # 缺失时服务启动失败——见 `secretbox.require_secret`。
    Migration(
        version=7,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS alert_events (
              id        INTEGER PRIMARY KEY,
              day       TEXT NOT NULL,
              metric    TEXT NOT NULL,
              value     REAL NOT NULL,
              threshold REAL NOT NULL,
              fired_at  TEXT NOT NULL,
              UNIQUE (day, metric)
            )
            """,
        ),
    ),
    Migration(
        version=8,
        statements=(
            # v8：实例设置（issue #11）的建表语句（详细说明见上一条迁移的注释）。
            """
            CREATE TABLE IF NOT EXISTS settings (
              key        TEXT PRIMARY KEY,
              value_json TEXT NOT NULL,
              encrypted  INTEGER NOT NULL DEFAULT 0 CHECK (encrypted IN (0, 1)),
              updated_at TEXT NOT NULL,
              updated_by TEXT REFERENCES users (id)
            )
            """,
        ),
    ),
    # v9：历史会话（问答页左栏）。会话严格按用户隔离（`user_id`），消息 `ON DELETE CASCADE`。
    # 每用户最多保留 200 个会话（新建时删最旧的，见 `conversations.prune`）。
    Migration(
        version=9,
        statements=(
            """
            CREATE TABLE IF NOT EXISTS conversations (
              id         TEXT PRIMARY KEY,
              user_id    TEXT NOT NULL REFERENCES users (id),
              title      TEXT NOT NULL,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS messages (
              id              TEXT PRIMARY KEY,
              conversation_id TEXT NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
              role            TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
              content         TEXT NOT NULL,
              citations_json  TEXT NOT NULL DEFAULT '[]',
              usage_json      TEXT,
              created_at      TEXT NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_conversations_user"
            " ON conversations (user_id, updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_messages_conversation"
            " ON messages (conversation_id, created_at)",
        ),
    ),
    # v10：`messages.version`（问答页历史引用的版本口径）。v9 的 `messages` 漏了版本，而引用的
    # `path` 是版本相关的（同一路径在 1.20.1 与 26.3 内容不同，甚至可能不存在），历史引用必须
    # 按当时的版本打开。**追加迁移**而不是回头改 v9：v9 可能已在生产库应用过。v9 期间落库的
    # 历史行为 NULL，前端遇到 null 回退到当前选中版本并在界面上明确提示。
    Migration(
        version=10,
        statements=("ALTER TABLE messages ADD COLUMN version TEXT",),
    ),
    # v11：删 `usage.cost_usd`（只审计 token，不算金额）。SQLite 3.35+ 的
    # `ALTER TABLE ... DROP COLUMN` 够用：`cost_usd` 上没有索引或约束。
    # **追加迁移**而不是回头改 v6：v6 已可能部署过；旧行里已有的金额一并丢弃。
    Migration(
        version=11,
        statements=("ALTER TABLE usage DROP COLUMN cost_usd",),
    ),
)

LATEST_VERSION: Final = MIGRATIONS[-1].version


def connect(path: Path) -> sqlite3.Connection:
    """打开控制面连接：WAL、外键、读行按列名。

    `isolation_level=None` 关掉隐式事务，写操作由 `transaction()` 显式 `BEGIN IMMEDIATE`，
    这样「先查再写」的判定（例如 setup 只能成功一次）不会是两步非原子的操作。

    `check_same_thread=False`：路由与依赖都是同步函数，被 FastAPI 丢到线程池里跑，
    而连接是由 async 生成器依赖建在事件循环线程上的；一个请求内连接只被顺序使用、
    不跨请求共享，所以放开线程检查是安全的（并且才能不阻塞事件循环）。
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")

    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """写事务。`BEGIN IMMEDIATE` 立刻拿写锁，并发写只会排队不会互相覆盖。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def _ensure_version_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
          version    INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL
        )
        """
    )


def schema_version(conn: sqlite3.Connection) -> int:
    """已应用的最大迁移版本；全新库返回 0。"""
    _ensure_version_table(conn)
    row = conn.execute("SELECT COALESCE(MAX(version), 0) AS version FROM schema_version").fetchone()

    return int(row["version"])


def migrate(conn: sqlite3.Connection) -> int:
    """把库迁移到最新版本并返回版本号。可重复调用（幂等）。"""
    current = schema_version(conn)

    for migration in MIGRATIONS:
        if migration.version <= current:
            continue

        # 重建表的迁移要先关外键，否则删旧表会连带清空子表（见 `Migration.foreign_keys_off`）。
        if migration.foreign_keys_off:
            conn.execute("PRAGMA foreign_keys=OFF")

        try:
            with transaction(conn):
                for statement in migration.statements:
                    conn.execute(statement)

                conn.execute(
                    "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                    (migration.version, utc_now()),
                )
        finally:
            if migration.foreign_keys_off:
                conn.execute("PRAGMA foreign_keys=ON")

    return schema_version(conn)


@dataclass(frozen=True)
class Database:
    """控制面数据库句柄：连接入口都走它，便于测试注入临时数据根。"""

    path: Path

    @classmethod
    def for_data_dir(cls, data_dir: Path) -> Database:
        return cls(path=data_dir / CONTROL_DB_NAME)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """每次新建连接（短连接），避免 sqlite3 的跨线程限制影响 FastAPI 依赖。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(self.path)
        try:
            yield conn
        finally:
            conn.close()

    def initialize(self) -> int:
        """建目录 + 迁移到最新版本，返回版本号。"""
        with self.connect() as conn:
            return migrate(conn)
