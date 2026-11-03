import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

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

  for (const child of children) {
    if (child.exitCode === null) child.kill('SIGTERM');
  }

  process.exit(code);
}

for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => shutdown(0));
}

const python = venvPython(API_DIR);

if (!python) {
  console.error('apps/api/.venv 不存在。先执行：bun run setup');
  process.exit(1);
}

if (!existsSync(join(ROOT, 'node_modules'))) {
  console.error('前端依赖未安装。先执行：bun run setup');
  process.exit(1);
}

console.log(`\u001b[36mapi\u001b[0m http://${HOST}:${PORT}   \u001b[36mweb\u001b[0m http://127.0.0.1:5173\n`);

spawnLogged(
  'api',
  '\u001b[32m',
  python,
  ['-m', 'uvicorn', 'mc_librarian.main:app', '--reload', '--host', HOST, '--port', PORT],
  { cwd: API_DIR }
);

spawnLogged('web', '\u001b[35m', BUN, ['run', 'dev'], {
  cwd: WEB_DIR,
  env: {
    ...process.env,
    MC_LIBRARIAN_API_URL: process.env.MC_LIBRARIAN_API_URL ?? `http://${HOST}:${PORT}`,
  },
});
