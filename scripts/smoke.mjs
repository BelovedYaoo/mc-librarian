/**
 * 冒烟测试：起 api 与 web（**全新临时数据根**），跑完首次启动的完整链路：
 *
 *   GET /v1/setup required=true → POST /v1/setup 建管理员 → 带 cookie GET /v1/auth/me
 *   → POST /v1/auth/logout → 旧 cookie 与匿名请求都 401
 *
 * 再断言经 vite 代理的 `/api/v1/health` 返回 `status: ok`（前端真的读到后端）。
 * 不依赖浏览器；临时数据根跑完即删，不碰开发者本机的 `mc-librarian-data/`。
 */
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { waitForHealth } from './lib/health.mjs';
import { killTree } from './lib/process.mjs';
import { venvPython } from './lib/python.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const API_DIR = join(ROOT, 'apps', 'api');
const WEB_DIR = join(ROOT, 'apps', 'web');
const BUN = process.versions.bun ? process.execPath : 'bun';

const HOST = process.env.MC_LIBRARIAN_HOST ?? '127.0.0.1';
const PORT = process.env.MC_LIBRARIAN_PORT ?? '8787';
const WEB_URL = process.env.MC_LIBRARIAN_WEB_URL ?? 'http://127.0.0.1:5173';

const API_BASE = `http://${HOST}:${PORT}`;
const API_HEALTH = `${API_BASE}/v1/health`;
const PROXIED_HEALTH = `${WEB_URL}/api/v1/health`;

// 每次都从空库开始：否则 setup 会在已有数据根上返回 410。
const DATA_DIR = mkdtempSync(join(tmpdir(), 'mc-librarian-smoke-'));

const COOKIE_NAME = 'mc_librarian_session';
const ADMIN = { username: 'smoke-admin', password: 'smoke-password-1' };

const children = [];
let shuttingDown = false;

function prefixed(prefix, color) {
  let buffer = '';

  return (chunk) => {
    buffer += chunk.toString();
    const lines = buffer.split(/\r?\n/);
    buffer = lines.pop() ?? '';

    for (const line of lines) {
      process.stdout.write(`${color}[${prefix}]\u001b[0m ${line}\n`);
    }
  };
}

function spawnLogged(name, color, command, args, { cwd, env = process.env }) {
  const child = spawn(command, args, { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });

  child.stdout.on('data', prefixed(name, color));
  child.stderr.on('data', prefixed(name, color));
  child.on('exit', (code) => {
    if (shuttingDown) return;
    console.error(`\u001b[31m${name} 提前退出（code=${code}）\u001b[0m`);
    shutdown(1);
  });

  children.push(child);

  return child;
}

function removeDataDir() {
  try {
    rmSync(DATA_DIR, { recursive: true, force: true, maxRetries: 5 });
  } catch {
    console.warn(`\u001b[33m清理临时数据根失败（可手动删）：${DATA_DIR}\u001b[0m`);
  }
}

function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;

  for (const child of children) killTree(child);

  removeDataDir();

  process.exit(code);
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => shutdown(1));
}

// ----------------------------------------------------------------------
// HTTP 断言小工具
// ----------------------------------------------------------------------

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

async function callApi(path, { method = 'GET', body, cookie } = {}) {
  const headers = { Accept: 'application/json' };

  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (cookie) headers.Cookie = cookie;

  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  const text = await response.text();
  let payload = null;

  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = text;
  }

  return { status: response.status, payload, headers: response.headers };
}

/** 从 201/200 的 Set-Cookie 里取出会话 cookie（Node 20+ 的 getSetCookie 更可靠）。 */
function readSessionCookie(headers) {
  const raw = typeof headers.getSetCookie === 'function' ? headers.getSetCookie() : [];
  const candidates = raw.length > 0 ? raw : [headers.get('set-cookie') ?? ''];

  for (const line of candidates) {
    const matched = new RegExp(`${COOKIE_NAME}=([^;]+)`).exec(line ?? '');

    if (matched) return `${COOKIE_NAME}=${matched[1]}`;
  }

  return null;
}

/** 首次启动 → 建管理员 → 会话 → 登出 → 401 的完整链路。 */
async function runAuthFlow() {
  const beforeSetup = await callApi('/v1/setup');
  console.log(`\u001b[32m✓\u001b[0m GET /v1/setup（全新库）→ ${JSON.stringify(beforeSetup.payload)}`);
  assert(beforeSetup.status === 200, `GET /v1/setup 期望 200，实际 ${beforeSetup.status}`);
  assert(beforeSetup.payload?.required === true, '全新数据根应该 required=true');

  const created = await callApi('/v1/setup', { method: 'POST', body: ADMIN });
  console.log(
    `\u001b[32m✓\u001b[0m POST /v1/setup → ${created.status} ${JSON.stringify(created.payload)}`
  );
  assert(created.status === 201, `POST /v1/setup 期望 201，实际 ${created.status}`);

  const cookie = readSessionCookie(created.headers);
  assert(cookie, 'POST /v1/setup 应该直接下发会话 cookie');
  console.log(`\u001b[32m✓\u001b[0m 拿到会话 cookie：${COOKIE_NAME}=…（长度 ${cookie.length}）`);

  const afterSetup = await callApi('/v1/setup');
  assert(afterSetup.payload?.required === false, '建完管理员后应该 required=false');
  console.log(`\u001b[32m✓\u001b[0m GET /v1/setup（已初始化）→ ${JSON.stringify(afterSetup.payload)}`);

  const secondSetup = await callApi('/v1/setup', {
    method: 'POST',
    body: { username: 'second-admin', password: 'second-password-1' },
  });
  assert(secondSetup.status === 410, `二次 setup 期望 410，实际 ${secondSetup.status}`);
  console.log(`\u001b[32m✓\u001b[0m 二次 POST /v1/setup → ${secondSetup.status}（已初始化）`);

  const me = await callApi('/v1/auth/me', { cookie });
  console.log(`\u001b[32m✓\u001b[0m GET /v1/auth/me（带 cookie）→ ${JSON.stringify(me.payload)}`);
  assert(me.status === 200, `带 cookie 的 /v1/auth/me 期望 200，实际 ${me.status}`);
  assert(me.payload?.username === ADMIN.username, '会话应解析出刚创建的管理员');

  const anonymous = await callApi('/v1/auth/me');
  assert(anonymous.status === 401, `匿名 /v1/auth/me 期望 401，实际 ${anonymous.status}`);
  console.log(`\u001b[32m✓\u001b[0m GET /v1/auth/me（匿名）→ ${anonymous.status}`);

  const logout = await callApi('/v1/auth/logout', { method: 'POST', cookie });
  assert(logout.status === 204, `POST /v1/auth/logout 期望 204，实际 ${logout.status}`);
  console.log(`\u001b[32m✓\u001b[0m POST /v1/auth/logout → ${logout.status}`);

  const afterLogout = await callApi('/v1/auth/me', { cookie });
  assert(afterLogout.status === 401, `登出后旧 cookie 期望 401，实际 ${afterLogout.status}`);
  console.log(
    `\u001b[32m✓\u001b[0m GET /v1/auth/me（登出后旧 cookie）→ ${afterLogout.status}` +
      ` · ${afterLogout.payload?.detail ?? ''}`
  );
}

// ----------------------------------------------------------------------

const python = venvPython(API_DIR);

if (!python || !existsSync(join(ROOT, 'node_modules'))) {
  console.error('依赖未就绪。先执行：bun run setup');
  process.exit(1);
}

console.log(`\u001b[36m==> smoke\u001b[0m api ${API_HEALTH} · web ${WEB_URL}`);
console.log(`\u001b[36m==>\u001b[0m 临时数据根 ${DATA_DIR}\n`);

spawnLogged('api', '\u001b[32m', python, ['-m', 'mc_librarian'], {
  cwd: API_DIR,
  env: {
    ...process.env,
    MC_LIBRARIAN_HOST: HOST,
    MC_LIBRARIAN_PORT: PORT,
    MC_LIBRARIAN_DATA_DIR: DATA_DIR,
    MC_LIBRARIAN_COOKIE_SECURE: 'false',
  },
});

spawnLogged('web', '\u001b[35m', BUN, ['run', 'dev'], {
  cwd: WEB_DIR,
  env: {
    ...process.env,
    MC_LIBRARIAN_API_URL: process.env.MC_LIBRARIAN_API_URL ?? `http://${HOST}:${PORT}`,
  },
});

try {
  const direct = await waitForHealth(API_HEALTH, { timeoutMs: 60_000 });
  console.log(`\u001b[32m✓\u001b[0m api 直连 ${API_HEALTH} → ${JSON.stringify(direct)}`);

  await runAuthFlow();

  const proxied = await waitForHealth(PROXIED_HEALTH, { timeoutMs: 60_000 });
  console.log(`\u001b[32m✓\u001b[0m 经 vite 代理 ${PROXIED_HEALTH} → ${JSON.stringify(proxied)}`);

  if (proxied.status !== 'ok') {
    throw new Error(`代理返回的 status 不是 ok：${JSON.stringify(proxied)}`);
  }

  // 深链要能进前端：dev server 对 /setup、/login 都得回 index.html（SPA fallback）。
  for (const route of ['/setup', '/login']) {
    const page = await fetch(`${WEB_URL}${route}`);
    const html = await page.text();

    assert(page.status === 200, `${route} 期望 200，实际 ${page.status}`);
    assert(html.includes('<div id="root">'), `${route} 应该返回 SPA 入口 HTML`);
    console.log(`\u001b[32m✓\u001b[0m web 深链 ${route} → ${page.status}`);
  }

  const db = join(DATA_DIR, 'control.sqlite');
  if (existsSync(db)) console.log(`\u001b[32m✓\u001b[0m 控制面数据库已生成：${db}`);

  console.log('\n\u001b[32msmoke 通过\u001b[0m');
  shutdown(0);
} catch (error) {
  console.error(`\n\u001b[31msmoke 失败\u001b[0m ${error instanceof Error ? error.message : error}`);
  shutdown(1);
}
