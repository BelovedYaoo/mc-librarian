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
packages/         （计划中）生成的 API 客户端、精简版 UI 组件库
deploy/           （计划中）Dockerfile / Caddyfile / compose
docs/              openapi.yaml（契约）、01-api.md、00-overview.md
scripts/          setup / dev / check / smoke（本地开发与校验入口）
```

## 状态

骨架可运行（issue #2）：后端 `/v1/health` + `/openapi.json`，前端空壳页面显示后端健康状态。
登录、索引、检索、问答等能力见 `docs/00-overview.md` §8 里程碑与对应 issue。

## 快速开始

前置：Python 3.13、Bun。首次执行：

```bash
bun run setup     # 建 apps/api/.venv、装后端与前端依赖
bun run dev       # 同时起 api(127.0.0.1:8787) 与 web(127.0.0.1:5173)，两端都热重载
```

打开 <http://127.0.0.1:5173/> 即可看到后端健康状态。前端只认 `/api` 前缀，开发期由
vite 代理到后端，生产期由 Caddy 反代；两部分都无需区分环境。

`bun run dev` 会自动补齐依赖（缺 `apps/api/.venv` 或 `node_modules` 时先跑 setup），
并在后端健康检查返回 `ok` 后打印 `ready`；60 秒内未就绪则报错退出。

校验（typecheck / lint / 契约漂移 / mypy / pytest）：

```bash
bun run check          # 默认跳过 smoke
bun run check:smoke    # 额外把 smoke（起真实端口）也跑一遍
bun run smoke          # 只跑冒烟测试
```

## 数据

源码与索引产物不入库。数据根由 `MC_LIBRARIAN_DATA_DIR` 指定，默认是
仓库**之外**的同级目录 `../mc-librarian-data/`（见 `docs/00-overview.md` §4）；
找不到仓库哨兵时退回 `~/mc-librarian-data/`。
