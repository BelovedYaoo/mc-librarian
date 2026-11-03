# mc-librarian · API

Python 3.13 + FastAPI + Pydantic v2。当前只实现 issue #2 的范围：`/v1/health` 存活探针与 OpenAPI 文档。

## 依赖与启动

```bash
# 仓库根
bun run setup     # 创建 apps/api/.venv 并安装依赖（也安装前端依赖）
bun run dev       # 同时起 api(127.0.0.1:8787) 与 web(127.0.0.1:5173)
```

单独跑后端（监听地址与端口取自 `Settings`，即 `MC_LIBRARIAN_HOST` / `MC_LIBRARIAN_PORT`）：

```bash
cd apps/api
.venv/Scripts/python -m mc_librarian --reload   # Windows
.venv/bin/python -m mc_librarian --reload      # macOS / Linux
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MC_LIBRARIAN_DATA_DIR` | `<repo 的父目录>/mc-librarian-data`（无仓库哨兵时 `~/mc-librarian-data`） | 数据根（源码 / 索引产物），永远在仓库之外；启动日志会打印最终路径 |
| `MC_LIBRARIAN_HOST` | `127.0.0.1` | 监听地址 |
| `MC_LIBRARIAN_PORT` | `8787` | 监听端口（与 `docs/openapi.yaml` 的本地 server 一致） |

## 校验

```bash
cd apps/api
.venv/Scripts/python -m pytest
.venv/Scripts/python -m mypy
.venv/Scripts/python -m ruff check .
```
