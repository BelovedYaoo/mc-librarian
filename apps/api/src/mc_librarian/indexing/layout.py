"""数据根里的索引与源码布局（规格补充二 §14）。

数据根下的目录：

- `<data_dir>/db/<version>.sqlite` —— 索引库，服务以 `mode=ro` 打开；
- `<data_dir>/src/<version>/` —— 源码树，`parse` 阶段就位，`outline`/`read` 实时读取；
- `<data_dir>/cache/<version>/` —— 下载与反编译的中间产物（client jar、官方 mappings、
  fetch checkpoint），由 `fetch` 阶段写入；
- `<data_dir>/jobs/`、`<data_dir>/logs/` —— 规格预留的任务工作目录与日志目录（规格「存储」一节）。

索引库里的 `files.path` 一律是相对 `<data_dir>/src/<version>/` 的 POSIX 相对路径，
因此「数据根」是服务侧唯一的事实来源（部署只需上传 `db/` 与 `src/` 两个东西）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

DB_DIR_NAME: Final = "db"
SRC_DIR_NAME: Final = "src"


def index_path(data_dir: Path, version: str) -> Path:
    """`<data_dir>/db/<version>.sqlite`。"""
    return data_dir / DB_DIR_NAME / f"{version}.sqlite"


def source_root(data_dir: Path, version: str) -> Path:
    """`<data_dir>/src/<version>/`：该版本的源码根。"""
    return data_dir / SRC_DIR_NAME / version
