# 取证：后端是否迁 TypeScript + `/v1/ask` 是否换 pi 作为 agent 核心

> 只读取证。未改动任何代码。取材时间：本机 pi `@earendil-works/pi-coding-agent` **1.5.0** 源码树
> （`C:\Users\ovo\.pi\agent\dev\pi-coding-agent-src\packages\coding-agent`）+ 本仓 `apps/api` + 官方文档。
> 引用说明：pi 的文档按 **文件路径 + 小节锚点** 引用（锚点是稳定标识，可 grep 到）；源码按 **文件 + 符号**
> 引用。本工具链读文件时不输出行号，故未逐条标行；**没有编造行号**——这一点在「未取证」里也记了一笔。

---

## 结论先行

1. **不要迁 TypeScript。** 唯一能说出口的收益是「能在同一进程里内嵌 pi SDK」，而 pi 有 RPC 模式且官方文档
   直接给了 Python 客户端示例（`docs/rpc.md#minimal-client`）——**用 Python 就能拿到 pi，不需要迁语言**。
   代价则是 16,361 行实现 + 13,120 行/593 条用例重写，且要同时替换 jieba / tree-sitter / tiktoken / pydantic /
   FastAPI DI 五处行为口径。
2. **不要把 `/v1/ask` 的循环换成 pi。** 不是因为 pi 不好，而是**我们循环里的三件事 pi 不管**：
   `degraded` 降级（模型不可用时用已召回结果兜底并仍返回 HTTP 200）、引用抽取、用量落库；再加一件
   **pi 另有主张**的：压缩策略。换过去等于把「我们实测调出来的产品行为」换成「上游的通用策略」，
   而收益只是少维护约 440 行的循环。
3. **pi 的安全红线是可关的，但要求三处同时关**（`noTools` / `tools` / `excludeTools`），且必须额外关掉
   扩展、技能、上下文文件发现——因为**扩展与 pi 同权限**。另一个反直觉点：pi 的 MCP 默认 exposure 是
   `codemode`（**不声明给模型**），所以「我们有 `/mcp` 端点」并**不等于**「pi 直接就能用这 5 个工具」，
   必须把 server 配成 `exposure: "direct"`。
4. **jieba 差异不是「跨语言契约风险」，而是「重建索引成本」。** 索引产物不入库（`AGENTS.md`），换分词器
   本来就要全量重建；只要索引侧与查询侧用**同一个**实现，命中就是自洽的。真正的红线是**混用**
   （Python 切索引 / JS 切查询）。这改写了迁移成本表里最贵的那一格。
5. **许可证不构成障碍**：pi 是 MIT，可商用、可自托管分发；OpenAI Agents SDK / PydanticAI / LangGraph
   也是 MIT，smolagents 是 Apache-2.0。**唯一的许可证雷在 Claude Agent SDK**：代码 MIT，但使用受
   Anthropic 商业条款约束，且绑 Claude 模型。

---

## 事实表

### A. pi 作为 agent 核心

| # | 事实 | 来源 | 类型 |
|---|---|---|---|
| A1 | 工具有四层开关：`noTools?: "all" \| "builtin"`（`"builtin"` 只关 read/bash/edit/write，**保留** extension/custom 工具）、`tools?: string[]`（白名单，支持 `*` 通配；**只有** `+name`/`-name` 时是「改默认」而不是白名单，混用会抛错）、`excludeTools?: string[]`（在 `tools` 之后生效，MCP 也受管）、`customTools?: ToolDefinition[]` | `src/core/sdk.ts` → `interface CreateAgentSessionOptions` 的 JSDoc；`docs/cli.md#tool-options` | 直接证据 |
| A2 | **全关一切**：`noTools: "all"` 或 `tools: []` 会把 built-in + extension + custom + MCP 全部关掉（源码里 `allowedToolNames = options.tools ?? (options.noTools === "all" ? [] : undefined)`，`defaultToolNames = options.noTools ? [] : ...`） | `src/core/sdk.ts` → `createAgentSession()` 工具选择段 | 直接证据 |
| A3 | 自建工具经 `createAgentSession({ customTools })` 传入时 `sourceInfo.source === "sdk"`；经扩展注册则是扩展身份 | `docs/extensions.md#pisetactivetools--pi--pi--pi`（`getAllTools()` 的 `sourceInfo` 说明段） | 直接证据 |
| A4 | 只经 MCP：SDK 会话**不会**自动加载内置扩展，必须自己加 `createMcpExtension()`；工具命名 `mcp__<server>__<tool>`；**默认 exposure 是 `codemode`**（可被 codemode 脚本调用，但**不声明给模型**）；要模型直接可见需 `exposure: "direct"` 或 `toolExposure` 覆盖 | `docs/sdk.md#codemode-mcp`、`docs/mcp.md#control-tool-exposure` | 直接证据 |
| A5 | `--tools` 只在条目以 `mcp__` 开头时才过滤 MCP 工具，否则「MCP 工具保留」——所以单靠 `tools: ["my_search"]` **不会**关掉 MCP 工具 | `docs/cli.md#mcp-tools`、`docs/sdk.md` 中 `tools` 的 JSDoc | 直接证据 |
| A6 | 扩展（以及经扩展注册的工具）**与 pi 同权限**，能读提示词、工具调用、文件、凭据、会话历史 | `docs/extensions.md` 开篇 Security 段（"An extension runs inside the Pi process with the same operating-system permissions"） | 直接证据 |
| A7 | 流式：订阅 `session.subscribe()`，`message_update` 的 `assistantMessageEvent.type` 取值 `start` / `text_start` / `text_delta`(`contentIndex`,`delta`) / `text_end` / `thinking_start` / `thinking_delta` / `thinking_end` / `toolcall_start`(`id`,`toolName`) / `toolcall_delta` / `toolcall_end`(`toolCall`) / `done` / `error` | `docs/json.md#message-events`（"Reconstruct streaming messages" 表）、`docs/sdk.md#subscribing-to-events` | 直接证据 |
| A8 | 工具事件：`tool_execution_start{toolCallId,toolName,args}` / `tool_execution_update{...,partialResult}` / `tool_execution_end{...,result,isError,durationMs}`；`message_end` 是权威终态；`agent_end` ≠ 结束，`agent_settled` 才是 | `docs/json.md#tool-execution-events`、`#event-sequence` | 直接证据 |
| A9 | 用量：`AssistantMessage.usage` 必有；`Usage = {input, output, cacheRead, cacheWrite, cacheWrite1h?, reasoning?, totalTokens, cost:{input,output,cacheRead,cacheWrite,total}}`；`reasoning` 已含在 `output` 里 | `docs/message-types.md#usage` | 直接证据 |
| A10 | 成本不是算出来的，是**查模型目录里的 cost 表**：自定义 provider 要自己在 `registerProvider`/`models.json` 的模型定义里填 `cost:{input,output,cacheRead,cacheWrite}`，不填就是 0 | `docs/extensions.md` → `pi.registerProvider` 的 `models[]` 示例含 `cost` 字段 | 直接证据 + 推断（"不填即 0" 属推断） |
| A11 | 压缩阈值：`contextTokens > contextWindow - reserveTokens`，`reserveTokens` 默认 **16384**；压缩时从后往前累计到 `keepRecentTokens`（默认 **20000**）为切点，在此之前的历史用 LLM 生成结构化摘要；结果是 `CompactionEntry{summary, firstKeptEntryId, tokensBefore, usage?}`；触发点在**工具批次结束、下一轮 assistant 之前**（`prepareNextTurn`），另有 overflow / `length` 的一次 compact-and-retry | `docs/compaction.md#when-it-triggers`、`#how-it-works`；实现 `src/core/compaction/compaction.ts`（`prepareCompaction()` / `compact()` / `shouldCompact()`） | 直接证据 |
| A12 | 压缩是**摘要不是丢弃**（原文 "Compaction adds a summary and keeps recent messages. It does not delete the original session entries."） | `docs/sessions.md#manage-conversation-context` | 直接证据 |
| A13 | 许可证 MIT；`author: Mario Zechner`；`engines.node >= 22.19.0`；`bin.pi = dist/bundle/cli.js` | `packages/coding-agent/package.json`（`license` / `engines` 字段） | 直接证据 |
| A14 | SDK 可在 Node 或 Bun 进程内；会话**不必落盘**：`SessionManager.inMemory()`、CLI `--no-session` | `docs/sdk.md#sessionmanager-api`、`docs/cli.md#session-options` | 直接证据 |
| A15 | 跨语言形态：`--mode rpc` 是长连 JSONL（stdin 命令 / stdout 响应+事件，**stdout 只放协议，日志走 stderr**）；`prompt` 的成功响应只表示「已接受/排队/被处理」，完成要看 `agent_settled`；文档自带 **Python 最小客户端**（`subprocess.Popen` + 按 LF 切字节流） | `docs/rpc.md#minimal-client`、`docs/cli-integration.md` | 直接证据 |
| A16 | 另有一过性形态：`--print`（只出最终文本）、`--mode json`（跑完即退的 JSONL） | `docs/cli-integration.md` 模式对照表 | 直接证据 |
| A17 | RPC 还内置 `bash` 命令（由**客户端**发送），与「模型可见的 bash 工具」是两件事 | `docs/rpc.md` 记录族表 + `#run-lifecycle` | 直接证据 |

### B. 本仓（取证用的事实）

| # | 事实 | 来源 | 类型 |
|---|---|---|---|
| B1 | jieba 的用途写在依赖注释里：摘要层 FTS5 预分词，「`trigram` 对两字词（「刷怪」）不友好，`unicode61` 又不切中文，所以入库前用 jieba 切好、查询时对 query 做同样切分」 | `apps/api/pyproject.toml` `dependencies` 中 `jieba>=0.42` 上方注释 | 直接证据 |
| B2 | tiktoken cl100k 是**验收口径**（`#13` 的 26.3 ≈ 7.5M token 是实测的），常数估算同批源码偏 13%–27%；`count_tokens` 是唯一入口，tiktoken 不可用时退回 3.5 字符/token 并 warn；lifespan 启动预热 | `apps/api/pyproject.toml`（tiktoken 注释）+ `apps/api/src/mc_librarian/indexing/text.py` 模块 docstring | 直接证据 |
| B3 | tree-sitter 钉 `<0.26`：0.26.0 的 Python 绑定遍历成员时堆损坏、真实大源码树随机段错误，见 issue #22 | `apps/api/pyproject.toml` `tree-sitter>=0.24,<0.26` 注释 | 直接证据 |
| B4 | 循环的三种收尾是产品行为：`done(reason=stop)` / `max_steps`（撤工具 + `FORCE_ANSWER_PROMPT`，注释说明「实测 deepseek 这类推理模型在无工具时会返回空 content」）/ `degraded`（模型不可用时用已召回结果兜底，HTTP 仍 200） | `apps/api/src/mc_librarian/answer/agent.py` 模块 docstring + `FORCE_ANSWER_PROMPT` 常量注释 | 直接证据 |
| B5 | 压缩策略是**目标水位**不是保留窗口：`COMPACT_TARGET_RATIO = 0.7`，注释解释「不能只压到刚好低于预算……留 30% 余量能把压缩间隔拉开到十几次调用一次」；另有 `MIN_RESULT_TOKENS=200`、`CONTEXT_BUDGET_TOOL` 伪工具记录丢弃 | `answer/agent.py` 同上常量段 | 直接证据 |
| B6 | 事件流形态：`step` / `delta` / `citation` / `done`，HTTP 层翻成 SSE 或一次性 `AskResponse` | `answer/agent.py` docstring | 直接证据 |
| B7 | 索引产物与源码**不入库**，默认在 `mc-librarian-data/` | `AGENTS.md`「约定」段 | 直接证据 |
| B8 | 依赖面：`fastapi>=0.118`、`pydantic>=2.12` + `pydantic-settings`、`mcp>=2.3`（FastMCP→MCPServer 2.x）、`argon2-cffi`、`cryptography`、`psutil`(dev, RSS 采样)、mypy strict | `apps/api/pyproject.toml` | 直接证据 |

### C. 「jieba 差异 = 契约风险」的重新定性

- 前提（已实测，来自任务方）：`@node-rs/jieba` 无 HMM，`cutHMM` 不存在；Python jieba 带 HMM 切出
  `["刷怪","上限",...]`，JS 切出 `["刷","怪","上限",...]`。→ **两种实现的切分不同，这是事实。**
- 推论（研究者推断，标注为推断）：FTS5 的命中要求**索引侧与查询侧一致**，不要求**与历史索引一致**。
  既然索引产物不入库（B7），换分词器本来就是一次全量重建；重建之后两侧都是 JS，检索是自洽的。
  所以这不是「无法跨语言」，而是「必须整体重建、且不能灰度混跑（新旧索引混用会静默降召）」。
- 该推断的反面条件：若真要灰度（一部分索引 Python 切、一部分 JS 切），则必须保留 Python 侧的
  `jieba.cut(HMM=False)` 与 JS 侧**逐词一致**——这一点**未验证**（`@node-rs/jieba` 在无 HMM 下是否与
  Python `cut(HMM=False)` 完全一致，没有证据）。

---

## 三条路线对照

成本区间为**研究者估算**（基于本仓模块行数 + 测试行数，未做计时实验），不当作事实。

| | **A. 保持 Python（现状）** | **B. Python + pi sidecar** | **C. 全迁 TypeScript** |
|---|---|---|---|
| **做法** | `answer/agent.py` 自维护循环不变 | Python 主进程 + `pi --mode rpc --no-session -nt`（或 `--mode json`）子进程；5 个工具经**现有 `/mcp`** 暴露给 pi（`exposure: "direct"`） | `apps/api` 全部重写为 TS（Fastify/Hono + bun:sqlite + zod/typebox） |
| **收益** | 零迁移；593 条用例继续有效；jieba/tree-sitter/tiktoken 口径不变；`degraded` 与 70% 压缩水位保持不变；mypy strict 的护栏在 | 白拿成熟循环：并行工具调用、自动重试、overflow/length 恢复、压缩、`agent_settled` 语义、扩展钩子（`tool_call` 可做权限门）；无需重写检索层 | 单语言单运行时；前后端可共享类型（但 `docs/openapi.yaml` 已是契约、前端类型已生成，**收益被现有架构吃掉大半**） |
| **成本** | 继续自维护 ~440 行循环 | 新运行时（Node ≥22.19）+ 新部署单元 + 子进程生命周期/超时/僵尸管理 + pi 侧 provider 配置（`models.json` 的 baseUrl/api/cost）+ 系统提示重做（pi 默认是 coding-agent 提示词，要 `forceSystemPrompt` 或重写 sections）+ **三件 pi 不管的事自己补**：`degraded` 兜底、引用抽取、用量落库 | 16,361 行实现 + 13,120 行/593 条用例重写（估 2–4 人月量级，含回归）；五处生态替换：jieba→无 HMM/或保留 Python 分词微服务、tree-sitter→`web-tree-sitter` wasm、tiktoken→`js-tiktoken`、pydantic→zod/typebox、FastAPI DI→手写装配 |
| **风险** | 循环演进靠自己；模型厂商 API 变更要自己跟 | **RCE 面**：bash/powershell/edit/write 要在 `noTools`+`tools`+`excludeTools` 三处同时关，且必须关闭扩展/技能/上下文发现（A6：扩展与 pi 同权限）；**MCP 默认 `codemode` 不声明给模型**（A4），不配 `direct` 会表现为「工具莫名不可见」；pi 的压缩策略（固定 reserve + 保留窗口，A11）与我们实测的 70% 水位（B5）**不是同一个策略**，换过去等于重调参；每会话多一个 Node 进程的内存（**未测**） | 分词/解析/token 口径漂移；索引全量重建（耗时未取证）；测试体系重建；一旦中途停手会成为双栈并存的长期税 |
| **可逆性** | 100% | **高**：旁路，删掉 sidecar 即回滚；不动 `/v1/ask` 契约 | **低**：中途停手=长期双栈 |

### 结论与否决理由

- **推荐 A（保持 Python，不引入 pi 作为核心）**，并把「成熟循环」的诉求降级为一个**可逆的 B 类实验**，
  不进主链路。理由：pi 能提供的能力，我们循环里真正缺的只有「并行工具调用」和「overflow 自动恢复」；
  而 pi **不能**提供 `degraded` 兜底、引用抽取、用量落库、以及我们实测调出来的 70% 压缩水位（B4/B5）。
  换句话说，接入 pi 之后，我们仍然要写这三到四段逻辑，还要多养一个运行时。
- **否决 C（全迁 TS）**：唯一的硬收益（进程内嵌 pi）被 A15 抹平；`openapi.yaml` 已经解决了跨语言类型
  契约问题，所以「前后端同语言」的收益在这里接近于零；成本/风险全压在分词、解析、token 三个口径上，
  而这三处**用户看不见、测试才发现**。为一个看不见的收益重写 29k 行是把风险放在最不划算的位置。
- **B 不是被否决，而是被限定**：只允许以「独立实验分支 + 不进 `/v1/ask` 主链路」的形式存在。它变成
  正确选择的触发条件只有一个——**产品要从「只读问答」变成「多轮代码探索 + 文件修改」**。那时 pi 的
  工具循环、权限钩子、压缩才真正值钱；现在的 5 个只读工具用不上它的强项。

---

## 最小验证实验（1–2 小时内可完成，且能证伪上面的判断）

> 每个实验都写成「若结果是这样 → 我不改主意 / 若那样 → 我改主意」。

**E1（60–90 分钟）pi sidecar 的可行性与安全边界——证伪「必须迁 TS 才能用 pi」**
1. 起本仓 `/v1/ask` 同源的 MCP 端点（现成的 `/mcp` + PAT）。
2. `~/.pi/agent/mcp.json` 里加该 server，`"exposure": "direct"`，`headers: {Authorization: "Bearer …"}`。
3. 用 `docs/rpc.md#minimal-client` 的 Python 示例连 `pi --mode rpc --no-session --no-builtin-tools`，
   发一句「Minecraft 里刷怪上限是怎么算的」。
4. 断言四件事：**(a)** `get_state`/`get_active_tools` 里只有 `mcp__*` 的 5 个工具（无 bash/read/write/edit）；
   **(b)** 能收到 `message_update.assistantMessageEvent.type == "text_delta"` 的增量；
   **(c)** `message_end.message.usage` 有 `input`/`output`/`totalTokens`（`cost` 可能因 `models.json` 未配 cost 而为 0 —— 正好验证 A10）；
   **(d)** 明确让模型「用 bash 跑 ls」，应得到 tool-not-found 而不是执行。
- **若 (a)(b)(c) 全过** → 「必须迁 TS」被证伪，C 路线失去最后的技术理由。
- **若 (d) 失败**（bash 仍可达）→ B 路线的安全论证不成立，直接排除 B。

**E2（60 分钟）jieba 的「自洽性」而非「一致性」——证伪「jieba 差异是迁移的硬阻塞」**
1. 抽 200 条真实中文摘要 + 200 条真实查询。
2. 三种组合：`Py-Py`（现状）、`JS-JS`（都用 `@node-rs/jieba`）、**`Py-JS`（混用，预期掉召）**。
3. 用同一套 FTS5 表结构、同一批 `MATCH` 查询，比 top-10 召回重合率。
- **若 JS-JS ≈ Py-Py 且 Py-JS 明显更差** → 证实我的推断（C4）：jieba 是「重建成本」不是「契约锁定」，
  成本表里最贵的一格可以下调；但**不因此改推荐**（因为 C 仍不划算）。
- **若 JS-JS 明显差于 Py-Py** → 我的推断错，jieba 的 HMM 对中文检索真有必要，则 C 路线的技术障碍升级为
  「要么保留 Python 分词微服务，要么换分词方案」，C 更不可行；B 路线也必须绕开 pi 的分词无关（B 不动索引，所以 B 不受影响）。

**E3（90 分钟）全迁成本的标定——证伪「成本估高了」或「估低了」**
1. 把一个中等模块（建议 `retrieval/` 里 300 行左右 + 其测试）用最顺手的 AI 辅助真译一遍到 TS + vitest。
2. 记录：耗时、行为差异条数、测试重写比例。
3. 另外统计：`tests/` 里直接依赖 pytest 特性（fixture / `monkeypatch` / parametrize / `caplog`）的用例占比——
   这个比例基本等于「593 条用例中有多少无法机械转写」。
- **若 300 行 + 测试 < 1 小时且零行为差异** → 我的成本估算偏高，C 值得重新算一遍（但仍需解释收益）。
- **若出现 ≥3 处行为差异或测试无法机械转写** → C 的估算成立，直接封盘。

---

## 未取证 / 不确定

1. **pi 单会话内存量级**：文档未给，未实测。B 路线若要谈「每会话一个 Node 进程」，这是必须补的数（可粗测：
   `pi --mode rpc --no-session` 空闲 RSS + 一轮问答后 RSS）。
2. **pi 1.5.0 的发布节奏与 API 稳定性**：只读了 `package.json` 版本号，没读 `CHANGELOG.md`，无法判断
   `createAgentSession` 这类接口的破坏性变更频率。这对 B 路线是实质性风险，**未取证**。
3. **本仓 `/mcp` 端点与 pi MCP 客户端的联调**：只依据 `docs/mcp.md`（支持 streamable HTTP、`headers` 可带
   `${ENV}`、拒绝 SSE、每请求 `timeout` 默认 60s、回包 >20KB 会截中间），**没有实测**。E1 就是补这个。
4. **一次全量索引耗时**（26.3 / 7.5M token）：未取证。它决定 C 路线「重建索引」这一格的真实成本。
5. **`web-tree-sitter` 在 Java 语法上的解析吞吐与 wasm 装配成本**：任务方只确认了「需要额外 wasm 装配」，
   没给性能数；issue #22 的绑定缺陷是否在 wasm 路径上重现也未知。
6. **`js-tiktoken` 吞吐**：只验证了结果一致（`public class A {}` → 4），**没测速度**；索引期 token 计数是热路径。
7. **593 条用例的 pytest 特性耦合比例**：未统计（E3 第 3 步补）。
8. **pi 文档按小节锚点引用而非行号**：本工具链读文件不输出行号，我没有编造行号；如需逐行可复核，
   用锚点 grep 即可定位到唯一小节。
9. **`@node-rs/jieba` 在无 HMM 下是否与 Python `cut(HMM=False)` 逐词一致**：无证据（E2 会顺带回答）。

---

## 其它候选（各一段结论；许可证 + 是否 Python 原生）

| 候选 | 许可证 | Python 原生 | 原生的工具循环 / 流式 / 用量 | 「只要工具不要 bash」 | 结论 |
|---|---|---|---|---|---|
| **OpenAI Agents SDK (Python)** | MIT（`pyproject.toml` 的 `license = "MIT"` + `LICENSE`） | 是 | 有 `Runner`（含 handoff、guardrail）；流式 `Runner.run_streamed()` + `RawResponsesStreamEvent` 逐 token、`RunItemStreamEvent` 给工具调用；用量 `result.context_wrapper.usage` = `requests` / `input_tokens` / `output_tokens` / `total_tokens` + `input_tokens_details.cached_tokens` / `output_tokens_details.reasoning_tokens`，**不含成本**（要自己乘单价） | **默认不挂本地执行工具**；但托管工具里有 `computerTool()` 与 `shellTool()`（**JS 文档明示 shellTool 有 local 模式与 hosted container 模式**），所以「不可能挂 bash」不成立，「默认安全、要显式开」成立 | 若真要换框架，它是 B/PydanticAI 之外的第三选项；缺点是成本口径要自己补，且 API 演进快（原 Swarm 血统） |
| **PydanticAI** | MIT（官方明示可商用） | 是（本仓已在用 pydantic 2.x / pydantic-settings） | 有原生工具循环（`@agent.tool`）；`run_stream()` / `run_stream_events()`；用量 `result.usage()`（`RunUsage`：input/output/total + 明细），成本经 `genai-prices` 定价后进 `usage.cost`，并可 `UsageLimits(cost_limit=…)`；MCP 客户端内置 | 是：工具只有你注册的；没有内置 bash/写文件工具 | **唯一「同语言、同进程、改造成本最低」的候选**；它与现状的差距主要是把 440 行循环换成框架的循环，收益是并行工具/重试/结构化校验，风险是 diff 铺满 `answer/`。可作为 B 的 Python 替代方案（比 pi sidecar 少一个运行时） |
| **LangGraph** | MIT（PyPI/README 标 MIT） | 是 | 有，但它是**图运行时**不是工具循环：1.x 起 `create_react_agent` 从 `langgraph.prebuilt` 迁到 `langchain.agents.create_agent`（`langgraph` 本体只剩图层）；流式 `astream(stream_mode="messages")`；用量来自各 provider 消息的 `usage_metadata`，**没有统一成本口径** | 是（工具由你提供） | 为「可审计状态机 / checkpoint / interrupt」而生；我们只有单轮问答 + 5 个工具，用它是过度工程，且引入 LangChain 依赖树 |
| **smolagents** | Apache-2.0（`LICENSE` + README 徽章） | 是 | `CodeAgent` 让模型**写 Python 代码**并执行；`ToolCallingAgent` 才是传统工具循环；流式是**步级**（`run(stream=True)` 出 step），不是 token 级 | 不是「有没有 bash」的问题，而是**它本身就是代码执行器**：默认 `LocalPythonExecutor` 在**本进程**里跑模型写的 Python，HF 官方明说「not fully isolated，不可信代码请用 Docker/E2B/Wasm」；且 `local_python_executor.py` 有在野 CVE（CVE-2026-4963，可远程触发的代码注入） | **多用户 Web 服务直接排除**：默认配置下这是「把 RCE 当成特性」 |
| **Claude Agent SDK (Python)** | 代码 MIT，但**使用受 Anthropic 商业条款约束**（官方 LICENSE/README 原话），且依赖 Claude Code CLI | 是（但底层是 Node CLI，`pip install` 已捆绑 CLI） | 完整 Claude Code 循环；流式需 `include_partial_messages=True` 才有 `StreamEvent`；`ResultMessage` 带 `total_cost_usd` 与累计 `usage`（官方 cost-tracking 文档） | **能**：`tools=[]` 关闭全部内置工具，`allowed_tools` / `disallowed_tools`（含 `Bash(rm *)` 这类作用域规则）+ `permission_mode` + `can_use_tool` 回调 | **排除**：绑定 Claude 模型（我们是 DeepSeek/OpenAI 兼容栈）、商业条款、且历史上 `allowed_tools=[]` 因真值判断被静默忽略（issue #634）——把一个安全开关交给一个会静默失效的参数，不合我们的风险偏好 |

---

## 如果一定要给一个「下一步」

1. 先做 **E1**（成本最低、信息量最大：同时回答「是否需要 TS」和「pi 是否安全可挂」）。
2. 再决定要不要花 1 天把 **PydanticAI 的 A/B** 做出来（用 `eval/` 现有评测跑同一批问题，比质量/时延/用量），
   这条比引入 Node 运行时便宜一个数量级。
3. **C 路线在当前信息下不值得开实验**；除非 E3 出现「零行为差异且 <1 小时/300 行」的反常结果。
