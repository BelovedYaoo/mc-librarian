# mc-librarian

Minecraft 源码知识库与中文问答服务。

- 三路混合检索：中文类摘要 / AST 符号与引用 / ripgrep 字面量
- 每个版本一个索引，`version` 显式传参，无隐式状态
- 账号体系（admin / user）、个人 access token、审计与用量记录
- 前端：版本管理 · 问答 · 检索调试台 · 任务队列 · 设置

## 目录

```
apps/api          Python：FastAPI + JobRunner + 索引 CLI
apps/web          React + Vite + MUI
packages/         生成的 API 客户端、精简版 UI 组件库
deploy/           Dockerfile / Caddyfile / compose
doc/              openapi.yaml（契约）、01-api.md、00-overview.md
```

## 状态

设计定稿，实现未开始。见 `doc/00-overview.md` §8 里程碑。

## 数据

源码与索引产物不入库，默认放在同级目录 `mc-librarian-data/`。
