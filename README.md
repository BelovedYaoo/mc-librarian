# mc-librarian

Minecraft 源码知识库检索服务。把反编译后的 MC 源码变成可被 agent 和人类检索的只读 HTTP API。

- 三路混合检索：中文摘要 / AST 符号 / ripgrep 字面量
- 无状态、多版本、显式 `version` 参数
- 核心是 HTTP + OpenAPI，MCP 只是可选适配器

## 目录

```
doc/
  openapi.yaml    接口契约（机器可读）
  01-api.md       API 说明与设计理由
  00-overview.md  架构、数据结构、决策记录
```

## 状态

设计阶段。索引与服务的实现尚未开始，见 `doc/00-overview.md` §7 里程碑。

## 数据

源码与索引产物不入库，见 `.gitignore`。
