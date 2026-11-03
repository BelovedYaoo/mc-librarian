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
| `MC_LIBRARIAN_JAVA` | `java` | 跑 Vineflower / SpecialSource 用的 `java` 可执行文件 |
| `MC_LIBRARIAN_VINEFLOWER_JAR` | 空 | Vineflower jar 路径（反编译必需） |
| `MC_LIBRARIAN_SPECIALSOURCE_JAR` | 空 | SpecialSource shaded jar 路径（≤ 1.21.11 的版本 remap 必需） |
| `MC_LIBRARIAN_DECOMPILE_MEMORY` | `4G` | Vineflower 的 `-Xmx`，形如 `4G` / `512M` |

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

## 下载与反编译（issue #21）

把某个版本的官方 client jar 变成一棵只含 `.java` 的源码树：

```bash
cd apps/api
# 26.x（Mojang 已不混淆，直接反编译）
MC_LIBRARIAN_VINEFLOWER_JAR=/path/vineflower-1.12.0.jar \
  .venv/Scripts/python -m mc_librarian fetch --version 26.3 --out ../mc-librarian-data/src/26.3

# ≤ 1.21.11（先下载官方 mappings 并 remap，再反编译）
MC_LIBRARIAN_VINEFLOWER_JAR=/path/vineflower-1.12.0.jar \
MC_LIBRARIAN_SPECIALSOURCE_JAR=/path/SpecialSource-1.11.5-shaded.jar \
  .venv/Scripts/python -m mc_librarian fetch --version 1.20.1 --out ../mc-librarian-data/src/1.20.1
```

安装后也可用 `mc-librarian fetch --version <v> --out <dir>`（同一个入口）。

- jar / mappings / checkpoint 默认缓存在 `<data_dir>/cache/<version>/`（`--cache` 可改）；
  中断后重跑会跳过已完成的阶段，不重复下载与反编译。`fetch.json` 里的完成状态也被
  `mc-librarian index` 读写（同一份），因此两个入口不会互相重复反编译。
- 是否 remap 由版本清单里有没有 `client_mappings` 决定，不比较版本号（26.x 与 1.14.4 之前的版本都没有）。
- 真实链路（联网 + 真反编译）的测试默认跳过：`.venv/Scripts/python -m pytest --run-network`。

## 端到端索引（issue #13）

一条命令从官方 jar 跑到可检索索引，产出 `db/<version>.sqlite` 并登记版本：

```bash
cd apps/api
MC_LIBRARIAN_VINEFLOWER_JAR=/path/vineflower-1.12.0.jar \
  .venv/Scripts/python -m mc_librarian index --version 26.3
```

- 不给 `--source-dir`：完整五段 `download → decompile → parse → summarize → finalize`；
- 给了 `--source-dir <dir>`：跳过 download/decompile，从 `parse` 开始（相对路径按当前目录解析）；
- 不给 `--force` 且索引已存在：跳过重建（复用已有索引）后继续收尾，重复执行不改变产物；
  `cache/<version>/fetch.json` 记着源码树已反编译完成时也跳过 `decompile`（与 `mc-librarian fetch`
  共用同一个完成标记），`--force` 一律重建；
- `summarize` 仍是占位（摘要由 #7 落地），本命令不调 LLM；
- 跑完版本行已登记（`db_path` 指向产出的索引），服务不需要再手工 `POST /v1/admin/versions`；
- 输出文件数 / 行数 / token / 内存峰值 / 耗时；内存峰值按秒采样**进程树** RSS（`psutil` 是 dev
  依赖，装不上只跳过测量并记一条 warn，不阻断）。

退出码：`0` 成功、`1` 任务失败（`error` 打到 stderr）、`2` 参数错误。

## 校验

```bash
cd apps/api
.venv/Scripts/python -m pytest
.venv/Scripts/python -m mypy
.venv/Scripts/python -m ruff check .
```
