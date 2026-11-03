# MC Librarian API · v0.1

> 机器可读契约见 [`openapi.yaml`](./openapi.yaml)。本文是人读版，解释「为什么这么设计」。

## 1. 定位

MC Librarian 是一个**只读**的代码知识库服务。它把一个 Minecraft 版本的反编译源码变成三路可检索的索引：

| 通道 | 数据来源 | 回答的问题 | 召回标记 |
|---|---|---|---|
| 摘要 | 每个类一句中文描述 + 关键英文标识符 | 「哪个类管这事」 | `summary` |
| 符号 | tree-sitter 解析出的类/方法/字段表 | 「这个符号在哪、谁在用」 | `symbol` |
| 字面量 | ripgrep 全文索引 | 「这串字面量/正则在哪」 | `literal` |

`POST /v1/search` 在**服务端**完成三路召回与融合，调用方（agent 或人）不需要知道该用哪条通道。这是刻意的：让模型做「选哪个工具」的决策，失败率远高于让它做「读结果、改写查询」。

## 2. 通用约定

- **无状态**：每个请求自带 `version`，服务端不保存会话。
- **版本显式**：没有「当前版本」这种隐式状态。多版本并存时这是唯一不出错的方式。
- **预算控制**：`max_tokens` 由调用方给，服务端按 score 降序截断。默认 4000。
- **错误格式**：RFC 7807 `application/problem+json`。
- **鉴权**：`Authorization: Bearer <token>`，token 来自环境变量 `MC_ATLAS_TOKEN`。内网可关闭（配置项）。

## 3. 端点

### 3.1 `POST /v1/search` — 混合检索

```json
{
  "query": "刷怪上限",
  "version": "1.20.1",
  "path_prefix": "net/minecraft/world",
  "kind": "any",
  "detail": "snippet",
  "limit": 10,
  "max_tokens": 4000
}
```

```json
{
  "query": "刷怪上限",
  "version": "1.20.1",
  "hits": [
    {
      "id": "1.20.1:net/minecraft/world/entity/MobCategory.java#MobCategory",
      "type": "class",
      "version": "1.20.1",
      "path": "net/minecraft/world/entity/MobCategory.java",
      "symbol": "net.minecraft.world.entity.MobCategory",
      "lines": [12, 88],
      "score": 0.87,
      "why": ["summary", "symbol"],
      "summary": "自然生成分类：定义怪物/动物/环境生物的最大数量上限、生成间距与每区块生成预算",
      "snippet": "public enum MobCategory {\n   MONSTER(\"monster\", 70, false, false, 128),",
      "tokens": 180
    }
  ],
  "suggestions": ["NaturalSpawner", "maxInstancesPerChunk", "spawnCategoryForChunk"],
  "total": 23,
  "took_ms": 8
}
```

**`why` 是给调用方模型看的导航信号**：`summary` 说明是语义命中（可以信摘要），`symbol` 说明是名字命中（可能同名不同义），`literal` 说明只是字面量巧合。不同来源的可信度不同，模型据此决定要不要 `read`。

**`suggestions` 是下一步的查询候选**：从同批命中的方法名/字段名里挑出区分度最高的几个。它把「查询改写」这件难事变成「挑一个建议」，显著降低弱模型空转的概率。

`detail=locate` 只返回 path + lines，用于「先定位、后精读」的两段式调用，比直接给片段省 5–10 倍 token。

### 3.2 `GET /v1/symbol` — 符号查找

```
GET /v1/symbol?version=1.20.1&name=MobCategory&fuzzy=true
```

返回 `SymbolDetail[]`：全限定名、签名、路径、行号、父类、接口、类摘要、成员列表。
`fuzzy=true` 时按「简单名 → 后缀匹配 → 编辑距离」三级降级。

### 3.3 `GET /v1/references` — 反查引用

```
GET /v1/references?version=1.20.1&symbol=MobCategory%23getMaxInstancesPerChunk
```

返回 `path + lines + enclosing(所在方法) + kind`。
数据来自 AST 启发式提取，**召回优先于精确**：覆盖调用、字段访问、类型声明三类，允许少量误报（由调用方 read 时证伪），但尽量不漏。

### 3.4 `GET /v1/outline` — 文件骨架

返回包名、imports、类/方法签名与行号，**不返回方法体**。这是 agent 最常用的第一步：先看骨架决定读哪段，避免整文件读入。

### 3.5 `GET /v1/read` — 源码切片

```
GET /v1/read?version=1.20.1&path=...&start=120&end=260&max_tokens=4000
```

`start`/`end` 必填。这是刻意的约束：不提供「读整个文件」的便利，因为对 5M token 的语料，整文件读取是上下文爆炸的主要来源。

### 3.6 `POST /v1/ask` — 服务端问答（可选）

```json
{ "question": "为什么 1.18 之后刷怪塔效率变低了？", "version": "1.20.1" }
```

服务端跑「检索 → 读取 → 作答」循环，返回中文答案 + `citations[]` + `steps[]`（工具调用轨迹）。
未配置生成模型时返回 `501`。

**它和 agent 无关**：任何 HTTP 客户端都能用，curl、网页、n8n、Dify 都行。这是「人类直接中文提问」的入口。

## 4. 三个接入方式

| 方式 | 适用 | 来源 |
|---|---|---|
| OpenAPI 3.1 | Dify / n8n / LangChain / 自研 | `doc/openapi.yaml` |
| MCP | Claude Code / Cursor / pi | 后续 `adapters/mcp.py`，薄封装上述端点 |
| 直接 HTTP | 自研 agent / 脚本 | 同上 |

MCP 只是适配器，不是架构的一部分。删掉它服务照常工作。

## 5. 未定项

见 `00-overview.md` 的「决策记录」。当前待确认的编号会在那里同步。

## 6. 变更记录

| 日期 | 版本 | 内容 |
|---|---|---|
| 2026-11-03 | 0.1.0 | 初稿：search / symbol / references / outline / read / ask |
