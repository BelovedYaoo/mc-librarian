# MC Librarian API · v0.2

> 机器可读契约见 [`openapi.yaml`](./openapi.yaml)（27 条路径 / 32 个操作，已通过 `openapi-spec-validator` 校验）。本文解释「为什么这么设计」。

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
| auth | `POST /v1/auth/login`、`POST /v1/auth/logout`、`GET /v1/auth/me`、`GET /v1/auth/tokens`、`POST /v1/auth/tokens`、`DELETE /v1/auth/tokens/{tokenId}`、`GET /v1/invites/{token}`、`POST /v1/invites/{token}/accept` | 公开 / 本人 |
| admin | `POST /v1/admin/versions`、`GET /v1/admin/users`、`POST /v1/admin/users`、`PATCH /v1/admin/users/{userId}`、`GET /v1/admin/jobs`、`POST /v1/admin/jobs`、`GET /v1/admin/jobs/{jobId}`、`GET /v1/admin/jobs/{jobId}/logs`、`POST /v1/admin/jobs/{jobId}/cancel`、`POST /v1/admin/jobs/{jobId}/retry`、`GET /v1/admin/settings`、`PUT /v1/admin/settings`、`GET /v1/admin/usage`、`GET /v1/admin/audit` | admin |

合计 27 条路径 / 32 个操作，与 `openapi.yaml` 一一对应。

## 4. 检索接口

### 4.1 `POST /v1/search`

```json
{ "query": "刷怪上限", "version": "1.20.1", "path_prefix": "net/minecraft/world", "limit": 10, "max_tokens": 4000 }
```

响应里两个字段是刻意设计的：

- **`why`**：`summary` 说明是语义命中（可以信摘要）、`symbol` 说明是名字命中（可能同名不同义）、`literal` 说明只是字面量巧合、`reference` 说明来自引用表反查到的调用点（召回优先、允许误报，`read` 可证伪）。模型据此判断要不要继续 `read`。
- **`suggestions`**：从同批命中的方法名/字段名里挑出区分度最高的几个，把「查询改写」变成「挑一个建议」。

`detail=locate` 只返回 `path + lines`，用于「先定位、后精读」的两段式调用，比直接给片段省 5–10 倍 token。
每条命中的 `tokens` 按**该条实际返回的内容**计：`locate` 只算定位字段（版本 / 路径 / 行区间 /
符号 / `why`），`snippet` 才把片段与摘要算进去；`max_tokens` 预算也按同一个数，否则 `locate`
的预算会被不存在的片段成本吃掉（实测 26.3：同一 query 下 `locate` 的 token 之和是 `snippet`
的 1/3–1/8.5，`max_tokens=400` 时能装下 12 条 vs 5 条）。计数口径是 `cl100k_base` 真分词。

**四路候选在服务端用 RRF 融合**：三路的分数量纲不可比（FTS5 的 bm25、符号匹配级别、ripgrep 无分数），
所以不做分数加权，而是 `score(d) = Σ 1/(60 + rank_c(d))`，每路各取前 50 条候选，再按本次最高分
归一化到 `[0, 1]`。同一实体被多路召回时累加，因此 `why` 会有多个值（顺序固定
`summary → symbol → literal → reference`）。`total` 是**融合前的候选总数**，不因 `limit` / `max_tokens`
截断而变。`max_tokens` 按 score 降序累加 `tokens`，超出预算即丢弃当前及之后的条目，但**至少保留第一条**。
`path_prefix` 在候选阶段过滤（三路各自带 path 条件），而不是融合之后。

字面量通道是 ripgrep 子进程（`--json --max-count 50 --max-columns 200 --max-filesize 2M --no-ignore`，
超时 5 秒）；超时或不可用时该路返回空并写 `warn`，不影响另外几路。引用通道在 query 命中某个符号名时
反查 `refs` 表；摘要通道查 `summaries_fts`（`#7` 建，FTS5；表不存在时返回空、不报错；表在但不是 FTS5、
或 query 分词后仍含 FTS 语法（`"`、`*`、`-`……）时同样返回空并写 `warn`，不把该路的问题升级成 500）。
query 与入库时一样先过 jieba 分词，否则两字词（「刷怪」）命中不了。

### 4.2 `GET /v1/symbol`

`fuzzy=true` 时按「简单名 → 后缀匹配 → 编辑距离」三级降级。返回签名、父类、接口、注解、类摘要、成员列表。

### 4.3 `GET /v1/references`

反查引用，返回 `path + lines + enclosing(所在方法) + kind`。数据来自 AST 启发式提取，**召回优先于精确**：允许少量误报（`read` 时会被证伪），但尽量不漏。

### 4.4 `GET /v1/outline` / `GET /v1/read`

`outline` 返回包名、imports、类与方法签名和行号，不含方法体——agent 最常用的第一步。
`read` 强制要求 `start`/`end`，**不提供整文件读取**：对 5M token 的语料，整文件读取是上下文爆炸的主要来源。

## 5. `/v1/ask`：SSE 事件序列

```
event: step      {"tool":"search","args":{"query":"刷怪上限","detail":"locate"}}
event: step      {"tool":"search","ok":true,"hits":3}
event: step      {"tool":"read","args":{"path":"...","start":120,"end":260}}
event: delta     {"text":"1.18 之后…"}
event: citation  {"path":"net/minecraft/world/entity/MobCategory.java","lines":[12,88],"symbol":"..."}
event: done      {"usage":{"prompt_tokens":18234,"completion_tokens":412,"cost_usd":0.0021,"latency_ms":9300},"reason":"stop"}
```

- **工具集固定 5 个**（`search` / `symbol` / `references` / `outline` / `read`），参数与上面四个检索端点同义；
  工具实现复用同一个检索服务层（`mc_librarian.retrieval`），不另写一套。`version` 由请求给出，模型改不了。
- `step` 让前端能把「正在检索什么」画出来，也让调用方知道答案的依据路径；同一个事件类型出现两次
  （调用前带 `args`，返回后带 `ok`/`hits`）。
- `citation` 与 `delta` 分离，前端可以把引用做成可点击的代码定位。
- `reason` 三种取值：`stop`（模型不再调工具）、`max_steps`（达到检索步数上限后强制收尾）、
  `degraded`（模型超时/不可用/返回非法响应）。
- **降级不返回 500**：模型不可用时用已召回的检索结果组装回答（第一步就失败则先跑一次
  `search` 拿位置），HTTP 仍是 `200`，`done.reason` 是 `degraded`。
- 请求头 `Accept: application/json` 时退化为一次性返回完整 `AskResponse`，方便脚本调用。
- 全局开关 `settings.ask.enabled=false` 时返回 `429`（异常时快速止血）；并发问答达到
  `settings.security.max_concurrent_asks` 时同样返回 `429`；未配置生成模型返回 `503`。
- 每次请求写**一行** `usage`（含降级的情况）：`cost_usd` 由 `MC_LIBRARIAN_LLM_INPUT_PRICE_PER_M` /
  `MC_LIBRARIAN_LLM_OUTPUT_PRICE_PER_M` 算，**未配单价时记 0**（只记 token、不计成本）。

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
- **建号**：管理员创建用户（`POST /v1/admin/users`，只给用户名与角色），返回**一次性邀请链接**（`/invite/<token>`，48 小时有效）；
  用户通过 `POST /v1/invites/{token}/accept` 自设密码并直接登录——管理员不需要也不应该知道别人的密码。
  未接受邀请的账号存的是占位哈希，任何密码都登录不进；链接用过即废，过期或账号被禁用同样返回 `404`。
- **角色**：`admin` / `user`，可提升可降级；**最后一个启用的 admin 不允许降级或停用**（`409`）。
  禁用账号会同时删除其全部会话，旧 cookie 立刻失效。
- **PAT**：每人可签发多个，单独吊销，展示 `last_used_at` 与前缀。
- **审计**：角色变更、禁用、创建用户、邀请创建/接受、PAT 签发/吊销、版本注册、任务创建/取消/重试、任务产出登记版本、设置修改全部落 `audit` 表
  （`GET /v1/admin/audit` 可按 `actor` 用户名筛选；前端 `/audit` 页按同一口径展示）。

密码用 **argon2id** 哈希存库（argon2-cffi 默认参数），接口与日志里都不会出现明文。

## 7. 管理接口

| 端点 | 说明 |
|---|---|
| `POST /v1/admin/versions` | 注册已上传的索引文件（节点侧不构建索引） |
| `GET/POST /v1/admin/users`、`PATCH /v1/admin/users/{id}` | 建号（返回一次性邀请链接）、升降角色、启停；最后一个启用的 admin 受保护 |
| `POST /v1/admin/jobs` | 创建索引任务（`kind` 目前仅 `index`），立即返回 `202 + job`，由 `JobRunner` 串行消费（并发 1） |
| `GET /v1/admin/jobs/{id}` | 任务状态：`stage` ∈ download/decompile/parse/summarize/finalize（`#4` 后三段，`#21` 前两段），`progress` 0–1 |
| `GET /v1/admin/jobs/{id}/logs` | 分页日志；用 `?after=<id>` 增量拉取，前端据此轮询 |
| `POST /v1/admin/jobs/{id}/cancel` | 只对 queued/running 有效，running 在当前阶段边界停止 |
| `POST /v1/admin/jobs/{id}/retry` | 只对 failed/canceled 有效：保留 checkpoint 重新排队，从下一未完成阶段续跑 |
| `GET/PUT /v1/admin/settings` | 模型、索引参数、会话时长、告警阈值；密钥 writeOnly。**`GET` 已实现（只读，值从 `MC_LIBRARIAN_*` 运行时配置派生，未实现的块整块省略）；`PUT` 与 `settings` 表随 #11 落地** |
| `GET /v1/admin/usage` | 按请求粒度记录，可按 day/user/version/model 聚合 |
| `GET /v1/admin/audit` | 审计日志 |

**任务只保留 `kind: index`**：`reindex` / `summarize` 是后续 ticket 的事，当前不提供，避免出现「跑成功但什么都没做」的任务。

**阶段序列由 `options.source_dir` 决定：**

| 入参 | 阶段序列 | 说明 |
|---|---|---|
| 带 `source_dir` | `parse → summarize → finalize` | `#4` 的真实路径：扫描源码目录、统计文件数与 token 估算 |
| 省略 `options` | `download → decompile → parse → summarize → finalize` | `#21` 的下载+反编译在前，接着 `#4` 的索引三段：解析版本清单、下载 jar（≤ 1.21.11 先 remap）、反编译出源码树，再就地解析成可检索索引 |
| 给了 `options` 但 `source_dir` 为空 | — | 422：空值属于笔误（要么省略 `options`，要么给非空目录） |

`download` / `decompile` 两段由 `#21` 实现（`mc-librarian fetch --version <v> --out <dir>` 是它的独立入口），产物（jar 路径、`.java` 文件数与字节数）写进 `jobs.checkpoint`（键为 `java_files`/`java_bytes`，与 `parse` 的 `files`/`source_size_bytes` 区分），续跑时跳过已完成阶段。省略 `options` 的任务从官方 jar 一路产出 `db/<version>.sqlite` 并登记版本，不需要人工衔接三步；`decompile` 产出的源码树就在 `parse` 消费的 `<data_dir>/src/<version>/`，`place_source_tree` 在源根相同时是 no-op。

**`download`/`decompile` 的完成状态以 `<data_dir>/cache/<version>/fetch.json` 为准**：`mc-librarian fetch` 与 `JobRunner` 写的是同一份（`JobRunner` 每跑完一段就并进去），因此两个入口共用同一个「源码树已就绪」的事实来源——该文件记着 `decompile` 已完成且 `<data_dir>/src/<version>/` 还在时，新任务不再重复反编译（`options.force=true` / `mc-librarian index --force` 一律重建；标记为真但源码树缺失或为空也重跑）。`jobs.checkpoint` 只负责**任务内部**的阶段续跑，两者不是一回事。

**索引构建闸门：** `MC_LIBRARIAN_INDEX_ENABLED=false`（默认 `true`）时本实例不构建索引，`POST /v1/admin/jobs` 与 `POST /v1/admin/jobs/{jobId}/retry` 一律回 `403`（`detail` 指向「本地生成 + `POST /v1/admin/versions` 注册」）；runner 启动后若闸门关闭，会把遗留的 `queued` 任务直接置为 `failed` 并写清原因（不静默丢弃）；`GET /v1/admin/settings` 的 `index.enabled` 就是这个开关。生产节点内存不够跑反编译，靠它把索引留在本地工作站。

**版本统计的权威来源：** `POST /v1/admin/versions` 注册的是管理员声明的权威索引；任务产出只在版本尚未注册时插入一行，**已注册版本的任何字段都不因任务而改变**。若管理员想用任务产出替换已注册版本，重新注册即可。

**版本写入分两条路径**：管理员 `POST /v1/admin/versions` 写全量字段（含 `db_path`/`has_summaries`）；任务产出仅在版本尚未注册时插入一行，并把产出的 `db_path`、`obfuscated` 与 `source_size_bytes`（`parse` 阶段写入）一并登记，`has_summaries` 按任务是否跑过 `summarize`（#7）写；已注册版本一个字段都不动。`obfuscated` 的取值：`options.obfuscated` 显式声明优先；未声明时跑过 `download` 用 checkpoint 里的真值，parse-only 缺省按「已 remap」（`true`）记录。`source_size_bytes` 的语义统一为**源码树字节数**（`.java` 与 `data/` 下 `.json` 的总字节数）：注册时若 `<data_dir>/src/<version>/` 不存在则**保留原值**（首次注册写 0），不拿索引 sqlite 的大小充数。

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
| 2026-11-03 | 0.2.0 | issue #10：契约补 `GET/POST /v1/auth/tokens`、`DELETE /v1/auth/tokens/{tokenId}`；实现 PAT 签发/列表/吊销与 `mcl_` 前缀 Bearer 鉴权，迁移 v2 落地 `pats` 表。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #9：契约补 `GET /v1/invites/{token}`、`POST /v1/invites/{token}/accept`，并把 `createUser` 的 `invite_url` 标为必填、给 `patchUser` 补 `404`；实现 users/audit/invites 与迁移 v3（`invites`/`audit` 两表）。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #4：契约补 `JobCreate.options.source_dir`、`/v1/admin/jobs*` 与 `/v1/admin/versions` 的 403/404/409/422；迁移 v4 落地 `jobs`/`job_logs`/`versions`，进程内 `JobRunner`（并发 1、阶段 checkpoint、阶段边界取消）随 lifespan 启动，`parse` 阶段统计文件数与 token 估算（字符 ÷ 3.5），版本注册与 `GET /v1/versions`；前端加 `/versions`、`/jobs` 页与侧边导航。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复：契约补 `POST /v1/admin/jobs/{jobId}/retry`、删掉 `getJobLogs` 的 SSE 描述、`source_dir` 改为所有 kind 必填；实现重试续跑与启动时重排遗留 `running`，`_succeed` 不再虚报 `obfuscated`/`has_summaries`，PAT 签发/吊销、任务创建、版本注册补审计，前端加 `/audit` 页。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（根因）：版本写入拆成管理员注册与任务产出两条路径（任务不再抹掉 `db_path`/`has_summaries`）；`kind` 收窄为 `index`（`reindex`/`summarize` 待 #7/#13），删掉无效的 `with_summaries`；阶段序列按 `options.source_dir` 选择；cancel/retry 与终态写入改为带状态守卫的原子 UPDATE（消除 TOCTOU），启动恢复不丢取消意图，`_fail` 加状态守卫；补 `version.recorded`/`job.canceled`/`job.retried` 审计，`audit` 的 `actor` 对系统动作为 `null`。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#4 收口）：任务只接受带 `options.source_dir` 的任务（缺则 422），阶段序列固定 `parse → summarize → finalize`，删掉 `download`/`decompile` 占位与死代码；任务产出改为「仅当版本未注册时插入一行」，已注册版本任何字段不变；取消的 `stage` 与 `message` 对齐（传当前阶段）、`_fail` 先写终态再按 rowcount 记日志、retry 重排清空旧阶段/进度/说明。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #21：迁移 v5 把 `jobs.stage` 的 CHECK 扩出 `download`/`decompile`，`STAGES` 扩为五段（阶段序列仍由 `options.source_dir` 选择，`parse`/`summarize`/`finalize` 行为不变）；新增 `mc-librarian fetch --version <v> --out <dir>`（Mojang 版本清单解析、下载 jar、≤ 1.21.11 用 SpecialSource 应用官方 mappings、Vineflower 反编译出只含 `.java` 的源码树，阶段产物写 checkpoint 供续跑）。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#4 收尾）：迁移 v4 的 `jobs.kind`/`stage` CHECK 收窄到接口层取值域（`kind` 仅 `index`；`stage` 为 NULL 或 `parse`/`summarize`/`finalize`），读取路径不再可能读到投影外的值而 500；取消时 `stage` 列补写当前阶段（重排会清空它），与 `message` 对齐；契约文档去掉内部函数名与「存储层保留历史值」的过期说法。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #5：`parse` 阶段真正落地——tree-sitter 解析 `.java`，把源码树就位到 `<data_dir>/src/<version>/`，并原子写出 `db/<version>.sqlite`（`files`/`classes`/`methods`/`fields`/`refs` 五表 + 索引；单文件失败跳过、失败率 > 20% 整体失败）；实现 `GET /v1/symbol`（fqn → simple → 后缀 → 编辑距离四级降级）、`GET /v1/references`（启发式引用反查，标出所在方法）、`GET /v1/outline`（包名 / imports / 类与方法签名与行号）、`GET /v1/read`（1-based 闭区间，超出 `max_tokens` 按行从尾部截断）；契约补四个端点的 401/404/422 并把 `Outline.package` 标为可空；前端加 `/browse` 检索调试台与侧边导航入口。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 集成修复（#21 × #5）：省略 `options` 的任务从官方 jar 跑完整五段 `download → decompile → parse → summarize → finalize`（不再只跑前两段），一次任务即产出可检索索引；`JobCreate.options.source_dir` 改为可选（给了 `options` 但值为空仍 422）；`decompile` 的 checkpoint 键改为 `java_files`/`java_bytes`，与 `parse` 的 `files`/`source_size_bytes` 区分；`run_stage` 对未知阶段报错而非静默产出空结果；源码根常量收敛到 `indexing.layout.source_root`。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#21 收尾）：任务产出的版本行补写 `db_path`（产出的 `db/<version>.sqlite`，确定性路径）、`obfuscated`（checkpoint 里 `download` 阶段的值）与 `source_size_bytes`（`parse` 阶段的值）——五段任务成功后 `/v1/symbol` 等直接可用，不再需要手工注册，`/v1/versions` 与检索端点不再自相矛盾；新增 `MC_LIBRARIAN_INDEX_ENABLED`（默认 `true`）闸门，关闭时 `POST /v1/admin/jobs` 回 403，`GET /v1/admin/settings` 落地为**只读**端点（`settings.index.enabled` 从运行时配置派生，未实现的块整块省略，`PUT` 与 `settings` 表随 #11）；契约补 `Settings.index.enabled` 与 `/v1/admin/settings` 的 403/描述；前端版本页「源码目录」改为可选（留空 = 从官方 jar 跑五段）并在关闭索引时隐藏「新建索引」入口。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#21 最后一轮）：`docs/00-overview.md` §4 的部署流程改掉不存在的 `mc-librarian index` 子命令（改为 `fetch` 取源码树 → 本地跑五段任务 → 上传 `db/<version>.sqlite` → `POST /v1/admin/versions` 注册）；`JobCreate.options` 新增可选 `obfuscated`（显式声明优先，未声明时 parse-only 缺省按「已 remap」记录）；`POST /v1/admin/versions` 的 `source_size_bytes` 改为源码树字节数（源码根不存在则保留原值，首次注册写 0）；`retry` 在闸门关闭时也回 403，runner 认领前清掉遗留 `queued` 任务并写清原因；启动时一次性回填历史遗留的 `db_path`（NULL 行且索引文件存在）；`Settings.index.java_xmx` 契约默认值改为实现的真实值 `4G`；版本页补「注册索引」入口。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#21 关闭前最后一轮）：`JobRunner._loop` 的异常兜底自身不再抛——`_fail` 失败只记 error 日志，单次失败不会让 asyncio 任务静默死亡、任务队列永久停摆；`_finish` 事务体去掉 `assert`（`python -O` 下会失效）并把 checkpoint 取值改成缺键/类型不对退回 0 的安全形式（保留单一事务，终态 + 版本登记 + 审计仍原子）；`start()` 的 `_recover_interrupted` 包异常，DB busy 不再阻断启动；文档订正 `POST /v1/admin/versions` 的 `source_size_bytes`（源码根不存在时保留原值，首次注册写 0）；契约补 `JobCreate.options.obfuscated` 显式传 `null` 返回 422。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #6：`POST /v1/search` 真正落地——四路召回（symbol / literal / reference，summary 留接口）用 RRF（k=60，每路前 50）融合、按最高分归一化；`literal` 走 ripgrep 子进程（固定参数 + 5 秒超时，不可用/超时降级为空并写 `warn`），`reference` 反查 `refs` 表，`summary` 表不存在时返回空；`why` 去重且顺序固定，`detail=locate` 不带 `snippet`/`summary`，`max_tokens` 按 score 降序截断但至少保留第一条，`path_prefix` 在候选阶段过滤，`suggestions` 按「排除 query 词 + 索引库出现次数升序」取前 5；契约补 `/v1/search` 的 401/404/422 与 `total` 语义；前端 `/browse` 升级为检索调试台（`why` 按通道着色、展开 `snippet`、建议可点击）。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | issue #13：新增端到端命令 `mc-librarian index --version <v> [--source-dir <dir>] [--force]`——复用 `JobRunner` 与 checkpoint，不给 `--source-dir` 跑完整五段、给了则从 `parse` 开始，跑完由 `_finish` 登记版本（`db_path` 指向产出索引，不需要再手工注册）；未给 `--force` 且索引已存在时跳过 `parse` 重建（CLI 置 `options.reuse_index`，API 任务不设该键、语义不变）；`download`/`decompile` 的完成状态收敛到 `<data_dir>/cache/<version>/fetch.json`（`JobRunner` 每跑完一段就并进去，与 `mc-librarian fetch` 共用同一份），该标记记着 `decompile` 已完成且源码树还在时新任务不再重复反编译（`force=true` 一律重建）；内存峰值按秒采样**进程树** RSS（`psutil` 为 dev 依赖，导入失败只降级为不测量并记 warn）；退出码三态（0 成功 / 1 任务失败 / 2 参数错误）；`summarize` 仍是占位（摘要由 #7 落地）。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-03 | 0.2.0 | 复审修复（#6 × #13）：token 口径改为 `cl100k_base` 真分词（`tiktoken` 进运行时依赖，常数估算降为 fallback 并写 warn）——`max_tokens` 预算与索引统计同一口径，与 #13 验收标准的实测值可比；`detail=locate` 的 `tokens` 按实际返回内容计（不再拿片段成本充数），预算也因此装得下更多条目；`Hit.id` 带上行区间（同名重载方法不再撞 id）；摘要通道整路包 `try/except sqlite3.Error`（表不是 FTS5、或 query 含 FTS 语法时返回空 + `warn`，不再 500）；`download` 也按 fetch 完成标记与 jar 存在性跳过联网（`--force` 除外）；CLI 被 Ctrl-C 中断时把任务标为 `failed`（不再留下永久 `running` 的行）；`psutil` 缺失的 warn 同时写进任务日志；ripgrep 记为 `apps/api` 的前置依赖，依赖它的测试在 `rg` 缺失时 skip。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-04 | 0.2.0 | 复审修复（#6 × #13 关闭前）：FastAPI lifespan 启动时预热一次 token 计数并把当前口径写进 INFO 日志（`token 计数：cl100k（tiktoken）` 或 `token 计数：字符估算 fallback（原因：…）`）——`tiktoken` 首次使用联网拉词表，生产节点拉不到时会阻塞到 HTTP 超时再静默退回常数估算，预热让漂移可见（生产镜像应预置 `TIKTOKEN_CACHE_DIR` 词表缓存，记入 #15）；摘要通道的 FTS 语法降级不再打 `exc_info`（`-`/`"` 这类随手输入不再刷整条 traceback）；CLI 的 Ctrl-C 收尾覆盖「已建 job 但未认领」窗口（`queued` 行也标 `failed`，不再留下永久 `queued`）；依赖 `literal` 通道的断言拆成独立 `@requires_ripgrep` 用例（无 `rg` 的机器上不再静默变红）；`docs/00-overview.md` §6 基线表补「内存峰值（实测）4.5 GiB」并订正 §4 的「3–4 GiB」。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-04 | 0.2.0 | issue #14：1.20.1 映射版本流水线的验收证据——`mc-librarian index --version 1.20.1` 复用 #13/#21 的既有实现直接跑通（实测 4,786 个 `.java`、557,300 行、5,560,214 cl100k token、内存峰值 4.5 GiB、53 秒，索引内 4,773 个文件 = 13 个 tree-sitter 解析失败被跳过）；`download` 阶段的复用判据补上「标记里记着用过官方 mappings 就必须还在」——缓存被删后重跑会重新取 mappings，取不到就报错点明版本与 mappings（不再把半份缓存当完成证据，也绝不退回反编译混淆 jar 产出 `a.java`）；补三类自动化测试：`files.path` 无 `a.java`/`ab.java` 形式且已知可读类名存在（离线跑，见 `tests/test_fetch.py`）、映射缺失报错（`tests/test_index_cli.py` 断言非 0 退出与错误文案）、1.20.1 与 26.3 两个索引共存时各自可查（`tests/test_symbols.py`）。属 v0.2 设计内的增量补齐，`info.version` 不变 |
| 2026-11-04 | 0.2.0 | issue #7：中文摘要层落地——新增 `mc_librarian.indexing.summarize`（LLM 只从 `MC_LIBRARIAN_LLM_BASE_URL`/`MC_LIBRARIAN_LLM_MODEL`/`MC_LIBRARIAN_LLM_API_KEY` 读，未配置时 `summarize` 阶段明确失败并给出可操作报错，不静默跳过）；按类内容哈希（FQN+父类+接口+字段签名+public 方法签名）幂等，只重跑变化的类；按 package 分批（`summary_batch_size`，默认 40）并发 `summary_concurrency`（默认 4）生成，单类失败保留其余、任务标 `failed`；索引库新增 `summaries`（普通表，带 `content_hash`）与 `summaries_fts`（FTS5 + jieba 预分词，两字词「刷怪」可命中），摘要通道真正接入融合（`why` 含 `summary`，不改 RRF）；任务产出登记版本时 `has_summaries` 按是否跑过 `summarize` 写；`/v1/versions` 的 `VersionInfo` 新增 `summary_count`/`class_count`（从索引库实时读，版本页显示摘要覆盖率），`has_summaries` 描述改为登记快照。属 v0.2 设计内的增量补齐，`info.version` 不变 |
