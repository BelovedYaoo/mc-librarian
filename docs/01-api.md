# MC Librarian API · v0.2

> 机器可读契约见 [`openapi.yaml`](./openapi.yaml)（24 条路径 / 29 个操作，已通过 `openapi-spec-validator` 校验）。本文解释「为什么这么设计」。

## 1. 定位

一个带账号体系的 Minecraft 源码知识库：检索接口给 agent 和前端用，管理接口给后台用，`/ask` 给人类直接提问用。

三路召回在**服务端**融合，调用方不需要知道该用哪条通道：

| 通道 | 数据来源 | 回答的问题 | 召回标记 |
|---|---|---|---|
| 摘要 | 每个类 3 行中文描述 + 关键英文标识符 | 「哪个类管这事」 | `summary` |
| 符号 | tree-sitter 解析的类/方法/字段表 + 引用表 | 「这个符号在哪、谁在用」 | `symbol` |
| 字面量 | ripgrep 全文检索 | 「这串字面量/正则在哪」 | `literal` |

## 2. 通用约定

- **无状态**：检索接口不自带会话，`version` 每个请求显式传，**不支持 `latest`**。
- **预算控制**：`max_tokens` 由调用方给，服务端按 score 降序截断。
- **错误格式**：RFC 7807 `application/problem+json`。业务错误与框架自己抛的 404 / 405 走同一套格式（405 保留 `Allow` 头）。
- **认证**：两种并列——浏览器用 `mc_librarian_session` cookie（HttpOnly + SameSite=Lax，`Secure` 由服务端 `MC_LIBRARIAN_COOKIE_SECURE` 控制），脚本用 `mcl_` 前缀的个人 access token。本地开发走 http，`Secure` 必须为 false，否则浏览器会直接丢弃 cookie；生产在 HTTPS 边界后面部署时必须为 true——`MC_LIBRARIAN_ENV=production` 而 `Secure` 为 false 时进程启动即失败。
- **授权**：`user` 可用检索与问答；`admin` 额外可用 `/v1/admin/*`。前端隐藏菜单只是体验，后端每个端点独立校验。

## 3. 端点总览

| 分组 | 端点 | 角色 |
|---|---|---|
| meta | `GET /v1/health`、`GET /v1/versions` | 公开 / 登录 |
| setup | `GET /v1/setup`、`POST /v1/setup` | 公开（仅首次启动可用） |
| retrieve | `POST /v1/search`、`GET /v1/symbol`、`GET /v1/references` | user |
| read | `GET /v1/outline`、`GET /v1/read` | user |
| answer | `POST /v1/ask`（SSE） | user |
| auth | `POST /v1/auth/login`、`POST /v1/auth/logout`、`GET /v1/auth/me`、`GET /v1/auth/tokens`、`POST /v1/auth/tokens`、`DELETE /v1/auth/tokens/{tokenId}` | 公开 / 本人 |
| admin | `POST /v1/admin/versions`、`GET /v1/admin/users`、`POST /v1/admin/users`、`PATCH /v1/admin/users/{userId}`、`GET /v1/admin/jobs`、`POST /v1/admin/jobs`、`GET /v1/admin/jobs/{jobId}`、`GET /v1/admin/jobs/{jobId}/logs`、`POST /v1/admin/jobs/{jobId}/cancel`、`GET /v1/admin/settings`、`PUT /v1/admin/settings`、`GET /v1/admin/usage`、`GET /v1/admin/audit` | admin |

合计 24 条路径 / 29 个操作，与 `openapi.yaml` 一一对应。

## 4. 检索接口

### 4.1 `POST /v1/search`

```json
{ "query": "刷怪上限", "version": "1.20.1", "path_prefix": "net/minecraft/world", "limit": 10, "max_tokens": 4000 }
```

响应里两个字段是刻意设计的：

- **`why`**：`summary` 说明是语义命中（可以信摘要）、`symbol` 说明是名字命中（可能同名不同义）、`literal` 说明只是字面量巧合、`reference` 说明来自引用表反查到的调用点（召回优先、允许误报，`read` 可证伪）。模型据此判断要不要继续 `read`。
- **`suggestions`**：从同批命中的方法名/字段名里挑出区分度最高的几个，把「查询改写」变成「挑一个建议」。

`detail=locate` 只返回 `path + lines`，用于「先定位、后精读」的两段式调用，比直接给片段省 5–10 倍 token。

### 4.2 `GET /v1/symbol`

`fuzzy=true` 时按「简单名 → 后缀匹配 → 编辑距离」三级降级。返回签名、父类、接口、注解、类摘要、成员列表。

### 4.3 `GET /v1/references`

反查引用，返回 `path + lines + enclosing(所在方法) + kind`。数据来自 AST 启发式提取，**召回优先于精确**：允许少量误报（`read` 时会被证伪），但尽量不漏。

### 4.4 `GET /v1/outline` / `GET /v1/read`

`outline` 返回包名、imports、类与方法签名和行号，不含方法体——agent 最常用的第一步。
`read` 强制要求 `start`/`end`，**不提供整文件读取**：对 5M token 的语料，整文件读取是上下文爆炸的主要来源。

## 5. `/v1/ask`：SSE 事件序列

```
event: step      {"tool":"kb_search","args":{"query":"刷怪上限","version":"1.20.1"}}
event: step      {"tool":"kb_read","args":{"path":"...","start":120,"end":260}}
event: delta     {"text":"1.18 之后…"}
event: citation  {"path":"net/minecraft/world/entity/MobCategory.java","lines":[12,88],"symbol":"..."}
event: done      {"usage":{"prompt_tokens":18234,"completion_tokens":412,"cost_usd":0.0021,"latency_ms":9300}}
```

- `step` 让前端能把「正在检索什么」画出来，也让调用方知道答案的依据路径。
- `citation` 与 `delta` 分离，前端可以把引用做成可点击的代码定位。
- 请求头 `Accept: application/json` 时退化为一次性返回完整 `AskResponse`，方便脚本调用。
- 全局开关 `settings.ask.enabled=false` 时返回 `429`，用于异常时快速止血。

## 6. 账号体系

- **首次启动**：`GET /v1/setup` 检测 `users` 表是否为空 → `required: true` 时前端强制进入 setup 向导；
  `POST /v1/setup` 创建唯一管理员，**成功即下发会话 cookie**（201），因此创建完直接进后台，
  不需要再登录一次。已初始化后 `GET` 恒为 `required: false`、`POST` 返回 `410`，不复用也不覆盖现有账号。
- **登录**：`POST /v1/auth/login` 换 `mc_librarian_session` cookie；服务端 `sessions` 表存 **token 的 sha256**，
  明文只存在浏览器 cookie 里。用户名不存在、密码错、账号被禁用一律 `401` + 同一句文案（
  「用户名或密码不正确」），且不存在的用户也会跑一次等价开销的 argon2 校验来抹平时序差。
  成功后更新 `last_login_at`。
- **会话**：`GET /v1/auth/me` 读当前用户，`POST /v1/auth/logout` 删服务端会话行并清 cookie；
  登出后旧 cookie 再用即 `401`——不是靠前端删 cookie 实现的。有效期默认 168 小时（`MC_LIBRARIAN_SESSION_TTL_HOURS`）。
- **建号**：管理员创建用户，返回**一次性邀请链接**（48 小时有效），用户自行设置密码——管理员不需要也不应该知道别人的密码。
- **角色**：`admin` / `user`，可提升可降级；**最后一个 admin 不允许降级或停用**。
- **PAT**：每人可签发多个，单独吊销，展示 `last_used_at` 与前缀。
- **审计**：角色变更、禁用、PAT 签发/吊销、设置修改、任务创建全部落 `audit` 表。

密码用 **argon2id** 哈希存库（argon2-cffi 默认参数），接口与日志里都不会出现明文。

## 7. 管理接口

| 端点 | 说明 |
|---|---|
| `POST /v1/admin/versions` | 注册已上传的索引文件（节点侧不构建索引） |
| `POST /v1/admin/jobs` | 创建索引任务，立即返回 `202 + job`，由 `JobRunner` 串行消费（并发 1） |
| `GET /v1/admin/jobs/{id}` | 任务状态：`stage` ∈ download/decompile/parse/summarize/finalize，`progress` 0–1 |
| `GET /v1/admin/jobs/{id}/logs` | 分页日志；`Accept: text/event-stream` 可增量跟随 |
| `POST /v1/admin/jobs/{id}/cancel` | 只对 queued/running 有效，running 在当前阶段边界停止 |
| `GET/PUT /v1/admin/settings` | 模型、索引参数、会话时长、告警阈值；密钥 writeOnly |
| `GET /v1/admin/usage` | 按请求粒度记录，可按 day/user/version/model 聚合 |
| `GET /v1/admin/audit` | 审计日志 |

**用量只记录、不设硬预算**：`usage` 表按请求记 `user / model / prompt_tokens / completion_tokens / cost_usd / latency_ms / version`；超阈值只推 Webhook 告警，是否暂停由管理员手动决定。

## 8. 三种接入方式

| 方式 | 适用 | 来源 |
|---|---|---|
| OpenAPI 3.1 | 前端生成的 TS 客户端、Dify / n8n / LangChain | `docs/openapi.yaml` |
| MCP | Claude Code / Cursor / pi | `apps/api` 内薄适配器 |
| 直接 HTTP | 脚本、curl | 同上 |

前端类型由 `openapi-typescript` 从本文件生成到 `apps/web/src/types/api.d.ts`（`bun run --cwd apps/web gen:api`）。**本仓没有 CI**，契约守卫是两道脚本/测试：`scripts/check.mjs` 的契约漂移守卫（重新生成类型，目录里的文件与生成结果不一致就失败）与 `apps/api/tests/test_contract.py`（比对运行时 `app.openapi()` 与本文件），防止「文档写了、代码没做」。

## 9. 变更记录

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-11-03 | 0.1.0 | 初稿：search / symbol / references / outline / read / ask |
| 2026-11-03 | 0.2.0 | 加账号体系、PAT、admin（users/jobs/settings/usage/audit）、ask 改 SSE、`latest` 移除 |
| 2026-11-03 | 0.2.0 | issue #3：契约补 `GET/POST /v1/setup`、`Session` 相关错误响应（`Unauthorized`/`Forbidden`/`SetupCompleted`）；实现 setup 与 login/logout/me，控制面 SQLite 落地 `users`/`sessions` + `schema_version`。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #4：契约补 `JobCreate.options.source_dir`、`/v1/admin/jobs*` 与 `/v1/admin/versions` 的 403/404/409/422；迁移 v2 落地 `jobs`/`job_logs`/`versions`，进程内 `JobRunner`（并发 1、阶段 checkpoint、阶段边界取消）随 lifespan 启动，`parse` 阶段统计文件数与 token 估算（字符 ÷ 3.5），版本注册与 `GET /v1/versions`；前端加 `/versions`、`/jobs` 页与侧边导航。属 v0.2 设计内的增量补齐，`info.version` 不变 |
