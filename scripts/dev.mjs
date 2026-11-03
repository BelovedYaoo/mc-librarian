import { spawn, spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { waitForHealth } from './lib/health.mjs';
import { killTree } from './lib/process.mjs';
import { venvPython } from './lib/python.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const API_DIR = join(ROOT, 'apps', 'api');
const WEB_DIR = join(ROOT, 'apps', 'web');

const HOST = process.env.MC_LIBRARIAN_HOST ?? '127.0.0.1';
const PORT = process.env.MC_LIBRARIAN_PORT ?? '8787';
const BUN = process.versions.bun ? process.execPath : 'bun';

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
    console.log(`\u001b[33m[${name}] 退出（code=${code}），停止另一边…\u001b[0m`);
    shutdown(code ?? 0);
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
  process.on(signal, () => shutdown(0));
}

// 一条命令到底：依赖缺失就地跑 setup，而不是让用户手动补一步。
if (!venvPython(API_DIR) || !existsSync(join(ROOT, 'node_modules'))) {
  console.log('\u001b[33m依赖未就绪（apps/api/.venv 或 node_modules 缺失），自动执行 setup…\u001b[0m');

  const setup = spawnSync(BUN, [join(ROOT, 'scripts', 'setup.mjs')], {
    cwd: ROOT,
    stdio: 'inherit',
  });

  if (setup.status !== 0) {
    console.error('自动 setup 失败，请修好上面的错误后重试：bun run setup');
    process.exit(setup.status ?? 1);
  }
}

console.log(`\u001b[36mapi\u001b[0m http://${HOST}:${PORT}   \u001b[36mweb\u001b[0m http://127.0.0.1:5173\n`);

spawnLogged('api', '\u001b[32m', venvPython(API_DIR), ['-m', 'mc_librarian', '--reload'], {
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

const healthUrl = `http://${HOST}:${PORT}/v1/health`;

try {
  const health = await waitForHealth(healthUrl, { timeoutMs: 60_000 });
  console.log(
    `\u001b[32mready\u001b[0m ${healthUrl} → ${JSON.stringify(health)}` +
      `\n        web http://127.0.0.1:5173/（页面会显示同一份健康状态）\n`
  );
} catch (error) {
  console.error(`\u001b[31m启动失败\u001b[0m ${error instanceof Error ? error.message : error}`);
  shutdown(1);
}
