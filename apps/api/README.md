# mc-librarian · API

Python 3.13 + FastAPI + Pydantic v2。当前实现 issue #2 + #3 + #9 的范围：`/v1/health`、首次启动 setup、登录/登出/当前用户、邀请链接的校验与接受、用户管理与审计（`/v1/admin/users`、`/v1/admin/audit`），以及 OpenAPI 文档。

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
| `MC_LIBRARIAN_DATA_DIR` | `<repo 的父目录>/mc-librarian-data`（无仓库哨兵时 `~/mc-librarian-data`） | 数据根（源码 / 索引产物 / 控制面 `control.sqlite`），永远在仓库之外；启动日志会打印最终路径 |
| `MC_LIBRARIAN_HOST` | `127.0.0.1` | 监听地址 |
| `MC_LIBRARIAN_PORT` | `8787` | 监听端口（与 `docs/openapi.yaml` 的本地 server 一致） |
| `MC_LIBRARIAN_ENV` | `dev` | `dev` / `production`。`production` 下会启用 cookie 硬性护栏（见下） |
| `MC_LIBRARIAN_COOKIE_SECURE` | `false` | 会话 cookie 的 `Secure` 属性。本地是 http，置 true 会让浏览器直接丢弃 cookie；**生产必须 true** |
| `MC_LIBRARIAN_SESSION_TTL_HOURS` | `168` | 会话有效期（小时） |
| `MC_LIBRARIAN_INVITE_TTL_HOURS` | `48` | 一次性邀请链接的有效期（小时） |

生产护栏：`MC_LIBRARIAN_ENV=production` 而 `MC_LIBRARIAN_COOKIE_SECURE` 仍为 false 时，
进程**启动即失败**并提示需要开 `Secure`——避免公网部署把会话 cookie 裸跑在 http 上。
本地开发不要设 `MC_LIBRARIAN_ENV=production`。

## 接口

| 端点 | 说明 |
|---|---|
| `GET /v1/health` | 存活探针（公开） |
| `GET /v1/setup` | `{ required: bool }`，控制面是否还没有任何用户（公开） |
| `POST /v1/setup` | 建唯一管理员并直接下发会话 cookie（201）；已初始化返回 410（公开） |
| `POST /v1/auth/login` | 登录，成功下发 `mc_librarian_session`（200）；失败统一 401 |
| `POST /v1/auth/logout` | 注销当前会话（204）；无有效会话 401 |
| `GET /v1/auth/me` | 当前用户（200）；未登录 401 |
| `GET /v1/invites/{token}` | 邀请链接是否仍可用（200，`{ valid, username? }`，公开） |
| `POST /v1/invites/{token}/accept` | 设密码 + 激活 + 直接登录（201）；链接无效/过期/已用 404（公开） |
| `GET /v1/admin/users` | 用户列表（200）；非 admin 403 |
| `POST /v1/admin/users` | 创建用户，返回一次性邀请链接（201）；用户名重复 409 |
| `PATCH /v1/admin/users/{userId}` | 改角色 / 启停（200）；最后一个启用的 admin 409 |
| `GET /v1/admin/audit` | 审计日志（200，可按 `actor` 筛选） |

控制面数据库 `<data_dir>/control.sqlite` 在启动时自动迁移（`schema_version` 表 + 幂等建表），
表结构定义见 `src/mc_librarian/db.py`。

## 校验

```bash
cd apps/api
.venv/Scripts/python -m pytest
.venv/Scripts/python -m mypy
.venv/Scripts/python -m ruff check .
```
