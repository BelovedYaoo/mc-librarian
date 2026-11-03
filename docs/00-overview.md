# MC Librarian · 设计总览

## 1. 一句话

把 Minecraft 各版本源码变成一个**带账号体系、HTTP 可检索、支持中文问答**的知识库，前端是管理后台 + 问答界面，后端是检索服务 + 索引流水线。

## 2. 非目标

- 不生成 wiki / 不做文档站
- 不做在线 IDE、不做代码跳转 UI
- 不索引 `assets/`（贴图、模型、lang）
- 不追求引用图 100% 精确（80% 召回即可，误报由 `read` 证伪）
- 不设自动预算上限（只记录用量 + 告警 + 手动熔断）

## 3. 仓库结构

```
mc-librarian/
├─ apps/
│  ├─ api/                 Python：FastAPI + JobRunner + 索引 CLI
│  └─ web/                 React + Vite + MUI 9（源自 minimal-vite-ts）
├─ scripts/                setup / dev / check / smoke（本地开发与校验入口）
├─ packages/               （计划中）
│  ├─ api-client/          （计划中）由 docs/openapi.yaml 生成
│  └─ ui/                  （计划中）精简版 react-core（删权限 manifest 与私有依赖）
├─ deploy/                 （计划中）Dockerfile.api / Dockerfile.web / Caddyfile / compose.yaml
├─ docs/
├─ package.json            bun workspaces
└─ pyproject.toml          （计划中）uv workspace
```

计划中的部分尚未落地：**当前仓库只有 `apps/api` + `apps/web` + `scripts`**。

## 4. 运行时

```
        ┌───────── web（内嵌 dist，容器内只跑 HTTP）───────────┐
:80  ───┤  静态站点 · /api/* 反代                                │
        └───────────────────────┬──────────────────────────────┘
                                │
                    ┌───────────▼───────────┐
                    │  api（Python 镜像）    │
                    │  FastAPI              │
                    │  JobRunner (asyncio)  │──► 子进程 java（反编译）
                    │  线程池（tree-sitter） │
                    └───────┬───────────────┘
                            │
              ┌─────────────┴──────────────┐
              │ control.sqlite (WAL, 读写)  │
              │ db/<version>.sqlite (只读)  │
              └────────────────────────────┘
```

TLS 在边界终止：边缘节点上的 `gateway-nginx` 持有泛域名证书，反代到 `deploy-net` 内的容器端口；
**web 镜像只提供静态文件与 `/api/*` 反代入口**（容器内只跑 HTTP，不持有证书）。

两条硬约束：

1. **索引文件只读**。服务以 `?mode=ro` 打开 `<version>.sqlite`；所有写入只发生在 `control.sqlite` 与 JobRunner 的临时工作区。
2. **检索逻辑只有一份**。`/v1/*`、MCP 适配器、`/v1/ask` 的工具调用都走同一个 `retrieval` 服务层。

### 4.1 部署拓扑

| 项 | 值 |
|---|---|
| 域名 | `mc-librarian.belovedyaoo.top` |
| 节点 | `47.122.113.116`（Debian 12，3.5 GiB 内存，Docker 29.5 / Compose 5.1.4） |
| 入口 | 节点上的 `gateway-nginx` 终止 TLS，反代到 `deploy-net` 内的容器端口 |
| 证书 | OHTTPS 自动续期的 `*.belovedyaoo.top` 泛域名 |
| 发版 | **不接 CI/CD**。发版频率低，手工 `docker compose up -d` + 网关 conf 变更 |

**索引不在节点上构建。** 节点只有 3.5 GiB 内存，反编译一个 5–7M token 的版本需要 3–4 GiB，硬跑会拖垮同机的其它服务。索引在本地工作站用 `mc-librarian index --version <v>` 生成，产物 `db/<version>.sqlite` 上传到节点数据根，再调 `POST /v1/admin/versions` 注册。节点侧的 `index.enabled` 默认关闭，版本页只显示状态与上传入口。

## 5. 数据模型

### 5.1 控制面 `control.sqlite`

| 表 | 关键字段 |
|---|---|
| `schema_version` | version, applied_at（迁移记账，启动时幂等补齐） |
| `users` | id, username, password_hash, role(admin/user), enabled, created_by, last_login_at |
| `pats` | id, user_id, name, token_hash, prefix, expires_at, last_used_at, revoked_at |
| `invites` | id, user_id, token_hash, expires_at, used_at |
| `sessions` | id, user_id, token_hash(sha256), created_at, expires_at, ip, user_agent |
| `jobs` | id, kind, version, status, stage, progress, message, error, checkpoint, options, cancel_requested, created_by, timestamps |
| `job_logs` | id, job_id, ts, level, message |
| `versions` | version, files, tokens, indexed_at, obfuscated, has_summaries, source_size_bytes, db_path |
| `usage` | id, user_id, kind, model, prompt_tokens, completion_tokens, cost_usd, latency_ms, version, created_at |
| `audit` | id, actor_id, action, target, detail_json, ip, created_at |
| `settings` | key, value_json, encrypted |

迁移机制刻意做得很薄（只用标准库 `sqlite3`，不引入 ORM）：`schema_version` 表记已应用的版本号，
每个迁移就是「版本号 + 一组 `CREATE ... IF NOT EXISTS`」，启动时按序补齐未应用的版本，因此重启幂等，
后续 ticket 加表只需在 `apps/api/src/mc_librarian/db.py` 的 `MIGRATIONS` 末尾追加一条。
密码存 **argon2id** 哈希，会话表只存 cookie token 的 **sha256**。

### 5.2 单版本索引 `db/<version>.sqlite`

```sql
files(id, path, pkg, loc, tokens, sha1)
classes(id, file_id, fqn, simple, kind, superclass, interfaces, annotations, start_line, end_line)
methods(id, class_id, name, signature, return_type, params, modifiers, annotations, start_line, end_line)
fields(id, class_id, name, type, modifiers, annotations, start_line, end_line)
refs(from_method_id, target_name, kind)          -- 启发式
summaries(fqn, text_zh, keywords, tokens)        -- FTS5, jieba 预分词
```

`summaries.text_zh` 固定 3 行：

```
自然生成分类：定义怪物/动物/环境生物的最大数量上限、生成间距与每区块生成预算
机制: 按生物群系权重挑选候选，受 mob cap 与玩家距离约束
关键成员: maxInstancesPerChunk, spawnInterval / getMaxInstancesPerChunk, getSpawnInterval
```

中文在前、英文标识符在后，一次检索两种语言都能命中同一行——这是不做跨语言 embedding 的替代方案。

## 6. 数据来源与实测体积

| 版本 | 方式 | 需要 remap |
|---|---|---|
| ≤ 1.21.11 | 官方 mappings + Vineflower | 是 |
| ≥ 26.x | 官方 jar 直接反编译（Mojang 已停止混淆） | 否 |

| 版本 | 文件 | 行数 | token |
|---|---|---|---|
| 1.12.2 | 2,050 | 27 万 | 3.9M（映射后估算） |
| 1.20.1 | 4,786 | 48 万 | 5.1M（映射后估算） |
| 1.21.8 | 6,105 | 56 万 | 6.0M（映射后估算） |
| 26.3 | 7,301 | 77 万 | **7.51M（实测）** |

## 7. 决策记录

| # | 决策 | 结论 |
|---|---|---|
| D1 | 检索引擎 | 不用向量库、不用 Zoekt；ripgrep + SQLite |
| D2 | 对外形态 | HTTP API 为主，MCP 只是薄适配器 |
| D3 | 版本隔离 | 一个版本一个索引，`version` 显式传递 |
| D4 | 中文入口 | 摘要行 = 中文 + 英文标识符混合 |
| D5 | 版本参数 | **仅单版本**，不提供 `latest` 别名 |
| D6 | 问答 | `/v1/ask` 必选，SSE 流式 |
| D7 | 鉴权 | 公网暴露；session cookie（浏览器）+ 个人 PAT（脚本） |
| D8 | 后端栈 | Python 3.13 + FastAPI |
| D9 | 数据布局 | 独立数据根 `mc-librarian-data/`，服务只读索引 |
| D10 | 项目名 | mc-librarian |
| D11 | 索引范围 | `.java` + `data/`，排除 `assets/` |
| D12 | 仓库形态 | 单仓：`apps/api` + `apps/web` + `packages/*` |
| D13 | 任务系统 | 进程内 `JobRunner` + 阶段检查点，并发 1，无独立 worker |
| D14 | 写入模型 | API 只写控制面 DB；`POST /admin/jobs` → 202 + job_id |
| D15 | 部署 | docker compose，2 容器（api、web）；TLS 由边缘节点 `gateway-nginx` 终止 |
| D16 | 前端底座 | React + Vite + MUI 9 + 精简版 react-core |
| D17 | 权限实现 | 自写 `RequireAuth` / `RequireAdmin`（`apps/web/src/routes/components/auth-guard.tsx`）；删除 react-core/authorization 与私有依赖 |
| D18 | 前端交付 | web 镜像内嵌 `dist`，只提供静态文件与 `/api/*` 反代入口；TLS/证书在边缘节点 `gateway-nginx` |
| D19 | 契约同步 | `openapi-typescript` 生成 `apps/web/src/types/api.d.ts`；无 CI，由 `scripts/check.mjs` 漂移守卫 + `tests/test_contract.py` 校验一致性 |
| D20 | 摘要粒度 | 类级 3 行，按 package 分批（30–50 个类/次） |
| D21 | 符号表 | 扩展字段（父类/接口/注解/参数名）+ 引用表 |
| D22 | 评测 | 人工 30 题为主，LLM 扩到 100 题做回归 |
| D23 | 用量 | 只记录不限额；Webhook 阈值告警 + 手动熔断 |
| D24 | 工具链瘦身 | 镜像内只要 JRE + Vineflower，不装 Gradle |

## 8. 里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| P1 | 源码流水线 | `src/26.3/`、`src/1.20.1/` |
| P2 | 符号索引 | `db/*.sqlite` 的 symbols + refs |
| P3 | 中文摘要 | `summaries` + FTS5 |
| P4 | HTTP 服务 + 账号 | `openapi.yaml` 全部端点 |
| P5 | 前端 | 版本管理 / 问答 / 检索调试台 / 任务队列 / 设置 |
| P6 | 评测集 | 30 题 golden set + 命中率报告 |
| P7 | 扩展版本 | 其余版本一键接入 |
