# MC Librarian · 设计总览

## 1. 一句话

把 Minecraft 各版本的源码变成一个**只读、无状态、HTTP 可检索**的知识库，供 agent 和人类用中文提问。

## 2. 非目标

- 不生成 wiki / 不做文档站
- 不做在线 IDE、不做代码跳转 UI
- 不索引资源文件（`assets/`），只索引代码与必要的数据文件
- 不追求引用图的 100% 精确（80% 召回即可，误报由 `read` 证伪）

## 3. 架构

```
                    ┌─────────────────────────────────────┐
   源码流水线         │  src/<version>/   反编译产物          │
   (离线, 一次性)     │        │                             │
                    │        ├─ tree-sitter  → symbols ──┐  │
                    │        ├─ ripgrep      → literal ──┤  │
                    │        └─ LLM 摘要     → summary ──┤  │
                    │                                   ▼  │
                    │                        db/<version>.sqlite
                    └─────────────────────────────────────┘
                                              │
                    ┌─────────────────────────▼───────────┐
                    │  服务层（只读，无状态）                 │
                    │  POST /v1/search  ·  GET /v1/read …  │
                    └───────┬──────────────┬──────────────┘
                            │              │
                     OpenAPI 3.1        MCP adapter
                   (Dify/n8n/自研)   (Claude Code/Cursor/pi)
```

三条硬约束：

1. **索引离线构建，服务只读。** 服务永远不写索引，避免并发写与锁。
2. **检索逻辑只有一份。** MCP / OpenAPI / `/ask` 都是薄封装，不允许各自实现一套。
3. **版本是一等公民。** 所有数据带 `version`，所有接口显式要求 `version`。

## 4. 数据结构（草案）

```sql
files(id, path, pkg, loc, tokens, sha1)
classes(id, file_id, fqn, simple, kind, superclass, interfaces, start_line, end_line)
methods(id, class_id, name, signature, return_type, modifiers, start_line, end_line)
fields(id, class_id, name, type, modifiers, start_line, end_line)
refs(from_method_id, target_name, kind)          -- 启发式
summaries(fqn, text_zh, keywords, tokens)        -- FTS5, jieba 预分词
```

`summaries.text_zh` 的格式是刻意的混合体：

```
自然生成分类：定义怪物/动物/环境生物的最大数量上限、生成间距与每区块生成预算
字段: maxInstancesPerChunk, spawnInterval  方法: getMaxInstancesPerChunk, getSpawnInterval
```

中文在前、英文标识符在后。一次检索，两种语言的查询都能命中同一行——这是不做跨语言 embedding 的替代方案。

## 5. 数据来源

| 版本 | 获取方式 | 是否需要 remap |
|---|---|---|
| ≤ 1.21.11 | MCP-Reborn（Gradle `setup` 任务） | 需要 |
| ≥ 26.x | 官方 jar 直接反编译（Mojang 已停止混淆） | 不需要 |

实测基线（Vineflower 1.12.0 + cl100k）：

| 版本 | 文件 | 行数 | token |
|---|---|---|---|
| 1.12.2 | 2,050 | 27 万 | 3.9M（映射后估算） |
| 1.20.1 | 4,786 | 48 万 | 5.1M（映射后估算） |
| 1.21.8 | 6,105 | 56 万 | 6.0M（映射后估算） |
| 26.3 | 7,301 | 77 万 | **7.51M（实测）** |

## 6. 决策记录

| # | 决策 | 状态 | 结论 |
|---|---|---|---|
| D1 | 不用向量库 / 不用 Zoekt | 已定 | 单版本 30MB 源码，ripgrep 全扫 30ms，SQLite 足够 |
| D2 | 核心是 HTTP API，MCP 只是适配器 | 已定 | 见 `01-api.md` §4 |
| D3 | 一个版本一个索引，`version` 显式传 | 已定 | 无状态 |
| D4 | 摘要行 = 中文 + 英文标识符混合 | 已定 | 替代跨语言 embedding |
| D5 | 版本参数是否支持跨版本联合检索 | **待定** | 建议 v1 不支持 |
| D6 | `/v1/ask` 是否进 v1 | 已定 | **必选**——前端问答页依赖它，且需流式 |
| D7 | 鉴权形态 | 已定（细节待定） | 公网暴露 + 单 Bearer token |
| D8 | 实现栈 | **待定** | 建议 Python 3.13 + FastAPI |
| D9 | 索引产物与数据目录 | **待定** | 引入前端后需重估 |
| D10 | 项目名 | 已定 | **mc-librarian** |
| D11 | 索引范围 | 已定 | `.java` + `data/`，排除 `assets/` |
| D12 | 产品形态 | 已定 | 前后端分离，含管理后台；控制面待设计 |

## 7. 里程碑

| 阶段 | 内容 | 产出 |
|---|---|---|
| P1 | 源码流水线 | `src/26.3/`、`src/1.20.1/` |
| P2 | 符号索引 | `db/*.sqlite` 的 symbols 部分 |
| P3 | 中文摘要 | `summaries` 表 + FTS5 |
| P4 | HTTP 服务 | `openapi.yaml` 全部端点 |
| P5 | 评测集 | 30 题 golden set + 命中率报告 |
| P6 | 扩展版本 | 其余版本一键接入 |
