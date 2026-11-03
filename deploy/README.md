# mc-librarian · 部署

issue #15 分两段：**段一**是本地产出部署物并在本地验证（本文档即段一的产物之一，已跑通）；
**段二**是节点部署，要动生产节点 `47.122.113.116` 上的共享网关，**属于运维动作，需人工确认后执行**，
本轮不执行，步骤见 [§3](#3-段二节点部署需人工确认后执行)。

## 1. 拓扑与产物

```
浏览器 ──HTTPS──► gateway-nginx（节点上，持有 *.belovedyaoo.top 泛域名证书）
                        │ 反代到 deploy-net 内的 mc-librarian-web:80
                        ▼
              web 容器（Caddy）：静态站点 + `/api/*` 反代
                        │ 容器内 HTTP，internal 网络
                        ▼
              api 容器（Python 3.13 + FastAPI）：/data 挂载数据根
```

TLS 在边界终止（D15/D18）：**两个容器都不持有证书**，web 只提供静态文件与 `/api/*` 反代入口。
api 只挂在 compose 的内部网络上，网关不直连 api。

| 文件 | 作用 |
|---|---|
| `deploy/Dockerfile.api` | api 镜像：Python 3.13 + JRE 17 + Vineflower + SpecialSource + ripgrep + 预置 tiktoken 词表 |
| `deploy/Dockerfile.web` | web 镜像：bun 构建 `apps/web/dist` → Caddy 运行 |
| `deploy/Caddyfile` | web 容器内的站点配置（静态站点、SPA 回退、`/api/*` 剥前缀反代） |
| `deploy/compose.yaml` | api + web 两个容器的编排、数据根挂载、网络、探活与重启策略 |
| `deploy/Dockerfile.*.dockerignore` | 构建上下文过滤器（上下文是仓库根，挡掉 `mc-librarian-data/`、`node_modules/` 等） |

镜像里有什么（`docker run --rm mc-librarian-api:local sh -c '...'` 实测）：

| 内容 | 版本 / 位置 |
|---|---|
| Python | 3.13（`python:3.13-slim-bookworm`） |
| JRE | OpenJDK 17.0.21 headless（`java -version`） |
| Vineflower | 1.12.0 → `/opt/mc-librarian/tools/vineflower.jar` |
| SpecialSource | 1.11.5 shaded → `/opt/mc-librarian/tools/specialsource-shaded.jar` |
| ripgrep | 13.0.0（`rg --version`） |
| tiktoken 词表 | `cl100k_base` 预置在 `/opt/mc-librarian/tiktoken-cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4`（1,681,126 字节） |

**不装 Gradle**（D24）：两个 jar 都是发布好的单个 jar，构建期用 `curl` 取进镜像，运行时不联网。

本地实测体积（`linux/amd64`）：api 589 MB、web 93.8 MB。

## 2. 段一：本地验证（已跑通）

前置：Docker（本地 Docker Desktop 即可）、能访问 Docker Hub / GitHub Releases / Maven Central /
`openaipublic.blob.core.windows.net`（只有构建期需要联网，运行期不需要）。

```bash
# 1) 外部网络 deploy-net（生产节点上已存在；本地首次需要建一次）
docker network inspect deploy-net >/dev/null 2>&1 || docker network create deploy-net

# 2) 起两个容器。端口用非默认值，避免与并行车道/本机服务冲突；
#    ENV/COOKIE_SECURE 显式降回 dev，否则会命中生产护栏（见 §4）
cd <仓库根>
MC_LIBRARIAN_ENV=dev MC_LIBRARIAN_COOKIE_SECURE=false \
MC_LIBRARIAN_DATA_DIR=../mc-librarian-data \
  docker compose -f deploy/compose.yaml up -d --build
```

验证清单（全部为实测结果）：

```bash
curl -s http://127.0.0.1:18787/v1/health          # {"status":"ok","version":"0.2.0"}
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18787/openapi.json   # 200（44,619 字节）
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:15173/               # 200（前端 index.html）
curl -s http://127.0.0.1:15173/api/v1/health      # 经 web 反代，剥掉 /api 前缀后同上
curl -s http://127.0.0.1:15173/api/v1/setup       # {"required":true}（前端真的连上了后端）

docker compose -f deploy/compose.yaml logs api | grep 'token 计数'
# 2026-11-03 17:53:48,363 INFO    mc_librarian: token 计数：cl100k（tiktoken）
docker exec mc-librarian-api-1 rg --version       # ripgrep 13.0.0
docker exec mc-librarian-api-1 java -version      # openjdk version "17.0.21"
```

词表预置的强证据：断网也能出 `cl100k`（不走 fallback）：

```bash
docker run --rm --network none mc-librarian-api:local \
  python -c "from mc_librarian.indexing.text import warmup_token_counting; print(warmup_token_counting())"
# token 计数：cl100k（tiktoken）
```

收尾：

```bash
docker compose -f deploy/compose.yaml down
```

## 3. 段二：节点部署（需人工确认后执行）

> **本轮不要执行。** 这一节要动共享网关 `gateway-nginx`（同机还承载其它服务），
> 且依赖 OHTTPS 证书链与 DNS 已就绪，属运维动作。

### 3.1 构建并推送镜像

节点只有 3.5 GiB 内存，**不要在节点上构建**（web 的 `vite build` 与 api 的 pip 安装都会吃内存）。
在本地构建后推送到节点可达的镜像仓库，或 `docker save` / `docker load`：

```bash
docker compose -f deploy/compose.yaml build
docker tag mc-librarian-api:local <registry>/mc-librarian-api:<tag>
docker tag mc-librarian-web:local <registry>/mc-librarian-web:<tag>
docker push <registry>/mc-librarian-api:<tag>
docker push <registry>/mc-librarian-web:<tag>
```

节点上把 `deploy/compose.yaml` 里的 `image:` 改成推上去的 tag，并把仓库放到节点（例如 `/opt/mc-librarian`）。

### 3.2 准备数据根

数据根放**仓库之外**（D9），例如 `/opt/mc-librarian-data`：

```
/opt/mc-librarian-data/
├─ control.sqlite        # 首次启动自动建表（schema 迁移幂等）
├─ src/<version>/        # 从本地工作站上传的源码树（可选，`outline`/`read` 用）
└─ db/<version>.sqlite   # 从本地工作站上传的索引，服务以 mode=ro 打开
```

索引**不在节点上构建**（节点内存不够，实测反编译峰值 4.5 GiB）：在本地工作站跑
`mc-librarian index --version <v>`，把产出的 `db/<version>.sqlite` 上传到节点数据根，
再调 `POST /v1/admin/versions` 注册。节点侧保持 `MC_LIBRARIAN_INDEX_ENABLED=false`
（compose 的默认值），服务进程内的索引构建会被闸门挡住（`POST /v1/admin/jobs` 返回 403）。

### 3.3 起容器

```bash
cd /opt/mc-librarian
MC_LIBRARIAN_DATA_DIR=/opt/mc-librarian-data \
  docker compose -f deploy/compose.yaml up -d
```

compose 的默认值就是生产口径，无需额外设置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `MC_LIBRARIAN_ENV` | `production` | `production` 下强制要求 cookie 有 `Secure` |
| `MC_LIBRARIAN_COOKIE_SECURE` | `true` | 与上一行配对；缺一个进程会**启动即失败** |
| `MC_LIBRARIAN_INDEX_ENABLED` | `false` | 节点不构建索引 |
| `MC_LIBRARIAN_DATA_DIR`（compose 变量） | `../../mc-librarian-data` | 宿主数据根，节点上显式传绝对路径 |
| `MC_LIBRARIAN_API_PORT` / `MC_LIBRARIAN_WEB_PORT` | `18787` / `15173` | 只绑 `127.0.0.1`，供本地排障 |

`restart: unless-stopped` 保证节点重启后容器自动恢复；api 带 `healthcheck`（`/v1/health`），
web 的 `depends_on` 等 api 健康后才起。

### 3.4 网关配置变更（`gateway-nginx`）

在节点的 `edge-conf.d/` 下新增 `mc-librarian.belovedyaoo.top.conf`，按**既有写法**（参考
`workbuddy.belovedyaoo.top.conf`，同样是「反代到 `deploy-net` 内某容器端口」）反代到
`mc-librarian-web:80`。要点：

- `server_name mc-librarian.belovedyaoo.top;`
- `proxy_pass http://mc-librarian-web:80;`（容器名 + 容器内端口，web 已在 `deploy-net` 上）
- 透传 `Host` / `X-Real-IP` / `X-Forwarded-For` / `X-Forwarded-Proto`，与既有文件保持一致
- 证书用 OHTTPS 自动续期的 `*.belovedyaoo.top` 泛域名，不单独签

改完按既有流程 `nginx -t` 后 reload（具体命令以节点上的既有流程为准，例如
`docker exec gateway-nginx nginx -t && docker exec gateway-nginx nginx -s reload`）。

### 3.5 注册索引并验收

```bash
curl -s https://mc-librarian.belovedyaoo.top/v1/health
# 登录拿会话后注册（文件数 / token 数由服务端从上传的索引里读，不用手填）：
curl -s -X POST https://mc-librarian.belovedyaoo.top/v1/admin/versions \
  -H 'Content-Type: application/json' -b cookies.txt \
  -d '{"version":"26.3","obfuscated":false}'
```

对应 issue #15 的验收标准：`/v1/health` 返回 ok、浏览器无证书告警、注册后公网可检索到真实代码、
`docker restart` 后容器自恢复、节点内存稳定、节点侧索引构建默认关闭。

## 4. 环境事实（避免重新踩坑）

- 节点：`47.122.113.116`（Debian 12，约 3.5 GiB 内存，Docker 29.5 / Compose 5.1.4）
- 外部 Docker 网络：`deploy-net`（compose 里声明为 `external: true`，本地验证前需手工建一次）
- 网关容器：`gateway-nginx`，配置在 `/opt/deploy/gateway/managed/versioned/nginx/conf.d/`
- 证书：OHTTPS 自动续期的 `*.belovedyaoo.top` 泛域名，已覆盖该子域
- 参考实现：`workbuddy.belovedyaoo.top.conf`
- 生产护栏：`MC_LIBRARIAN_ENV=production` 而 `MC_LIBRARIAN_COOKIE_SECURE=false` 时进程启动即失败并
  提示开 `Secure`。本地验证必须把这两个值显式降回 `dev` / `false`，否则容器起不来：

  ```
  配置错误，服务未启动：
    - settings：生产环境必须给会话 cookie 加 Secure：请设置 MC_LIBRARIAN_COOKIE_SECURE=true。
  ```

- tiktoken 词表已在**镜像内**预置（`TIKTOKEN_CACHE_DIR=/opt/mc-librarian/tiktoken-cache`），
  运行时不依赖 `openaipublic.blob.core.windows.net`；节点拉不到词表时才会退回「字符 ÷ 3.5」估算，
  那会让启动日志出现 `token 计数：字符估算 fallback（原因：…）` 并改变 `max_tokens` 的语义。
