# AGENTS.md

本仓库是 mc-librarian：Minecraft 源码知识库与中文问答服务。后端 `apps/api`（Python / FastAPI），前端 `apps/web`（React / Vite / MUI）。

## Agent skills

### Issue tracker

Issues live as GitHub issues in `BelovedYaoo/mc-librarian`, managed with the `gh` CLI; external PRs are not a triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical labels with default names: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` at the repo root plus `docs/adr/`. See `docs/agents/domain.md`.

## 约定

- API 契约以 `docs/openapi.yaml` 为准，前端类型由它生成，不允许手写漂移。
- 源码与索引产物不入库，默认在 `mc-librarian-data/`。
- 提交信息用中文。
