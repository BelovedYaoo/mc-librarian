# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

## Layout: single-context

This repo is **single-context**. One domain — the Minecraft code knowledge base — with the web app as its presentation layer, not a separate bounded context.

```
/
├── CONTEXT.md          ← domain glossary (created lazily)
├── docs/adr/           ← architectural decisions
├── apps/api/           ← Python service (FastAPI + JobRunner + index CLI)
└── apps/web/           ← React admin + chat UI
```

## Before exploring, read these

- **`CONTEXT.md`** at the repo root.
- **`docs/adr/`** — read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill creates them lazily when terms or decisions actually get resolved.

## Use the glossary's vocabulary

When output names a domain concept (issue title, refactor proposal, hypothesis, test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly avoids.

Terms currently in use and their meanings — until `CONTEXT.md` exists, treat these as authoritative:

- **version** — a Minecraft release being indexed (e.g. `1.20.1`, `26.3`). Always explicit; there is no `latest`.
- **index** — the per-version SQLite artifact produced by an indexing job.
- **job** — one indexing run. Stages are `download → decompile → parse → summarize → finalize`; `#21` supplies `download`/`decompile` (official jar → source tree) and `#4` the indexing three. A job with an explicit `source_dir` skips the first two and starts at `parse`.
- **stage** — a checkpointed phase of a job; a restart resumes at the next unfinished stage.
- **registered version** — a version row written by an administrator via `POST /v1/admin/versions`; its fields are authoritative and are never overwritten by a job.
- **job version** — a version row inserted by a job when no registered version exists yet; later replaced by an administrator registration.
- **summary** — the three-line Chinese description of a class, plus its key English identifiers.
- **symbol** — a class, method, or field extracted from the AST.
- **hit** — one retrieval result, carrying `why` (which channel recalled it) and a line range.
- **why** — the recall channel(s) for a hit: `summary`, `symbol`, `literal`, `reference`, `patch`.
- **suggestion** — a follow-up symbol name offered to the caller to rewrite its query.
- **PAT** — personal access token, `mcl_` prefixed, per user, revocable.

## Flag ADR conflicts

If output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0007 (event-sourced orders) — but worth reopening because…_
