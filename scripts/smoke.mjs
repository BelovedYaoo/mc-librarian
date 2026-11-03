/**
 * 冒烟测试：起 api 与 web，等就绪，断言经 vite 代理的 `/api/v1/health` 返回 `status: ok`，再关掉。
 *
 * 这是「前端真的能读到后端」的端到端证据；不依赖浏览器。
 */
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
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

const API_HEALTH = `http://${HOST}:${PORT}/v1/health`;
const PROXIED_HEALTH = `${WEB_URL}/api/v1/health`;

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

function shutdown(code) {
  if (shuttingDown) return;
  shuttingDown = true;

  for (const child of children) killTree(child);

  process.exit(code);
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => shutdown(1));
}

const python = venvPython(API_DIR);

if (!python || !existsSync(join(ROOT, 'node_modules'))) {
  console.error('依赖未就绪。先执行：bun run setup');
  process.exit(1);
}

console.log(`\u001b[36m==> smoke\u001b[0m api ${API_HEALTH} · web ${WEB_URL}\n`);

spawnLogged('api', '\u001b[32m', python, ['-m', 'mc_librarian'], {
  cwd: API_DIR,
  env: { ...process.env, MC_LIBRARIAN_HOST: HOST, MC_LIBRARIAN_PORT: PORT },
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

  const proxied = await waitForHealth(PROXIED_HEALTH, { timeoutMs: 60_000 });
  console.log(`\u001b[32m✓\u001b[0m 经 vite 代理 ${PROXIED_HEALTH} → ${JSON.stringify(proxied)}`);

  if (proxied.status !== 'ok') {
    throw new Error(`代理返回的 status 不是 ok：${JSON.stringify(proxied)}`);
  }

  console.log('\n\u001b[32msmoke 通过\u001b[0m');
  shutdown(0);
} catch (error) {
  console.error(`\n\u001b[31msmoke 失败\u001b[0m ${error instanceof Error ? error.message : error}`);
  shutdown(1);
}
