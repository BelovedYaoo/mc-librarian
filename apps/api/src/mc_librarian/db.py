"""控制面 SQLite（`<data_dir>/control.sqlite`）：连接管理与最小迁移机制。

设计约束（见 `docs/00-overview.md` §5.1）：

- 只用标准库 `sqlite3`，不引入 ORM；
- 迁移只有「版本表 + 幂等建表」：每个迁移是一个版本号加一组 `CREATE ... IF NOT EXISTS`，
  启动时把 `schema_version` 里没有的版本按序执行一遍并记账，因此重复启动是安全的；
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
    """一次 schema 迁移：版本号 + 一组幂等语句。"""

    version: int
    statements: tuple[str, ...]


# v1：首个 schema——账号与会话。#9 加 invites/pats、#4 加 jobs 时往后追加即可。
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
)

LATEST_VERSION: Final = MIGRATIONS[-1].version


def connect(path: Path) -> sqlite3.Connection:
    """打开控制面连接：WAL、外键、读行按列名。

    `isolation_level=None` 关掉隐式事务，写操作由 `transaction()` 显式 `BEGIN IMMEDIATE`，
    这样「先查再写」的判定（例如 setup 只能成功一次）不会是两步非原子的操作。
    """
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
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

        with transaction(conn):
            for statement in migration.statements:
                conn.execute(statement)

            conn.execute(
                "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
                (migration.version, utc_now()),
            )

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
