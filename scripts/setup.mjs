import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { findSystemPython, venvPython } from './lib/python.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const API_DIR = join(ROOT, 'apps', 'api');

function step(label) {
  console.log(`\n\u001b[36m==> ${label}\u001b[0m`);
}

function run(command, args, options = {}) {
  const result = spawnSync(command, args, { stdio: 'inherit', cwd: ROOT, ...options });

  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}

step('定位 Python 3.13 解释器');

let python = venvPython(API_DIR);

if (python) {
  console.log(`复用已有 venv：${python}`);
} else {
  const system = findSystemPython();

  if (!system) {
    console.error(
      '找不到 Python 3.13。请安装 Python 3.13，或用 MC_LIBRARIAN_PYTHON 指向解释器路径。'
    );
    process.exit(1);
  }

  step(`创建 apps/api/.venv（${system.command}）`);
  run(system.command, [...system.args, '-m', 'venv', join(API_DIR, '.venv')]);
  python = venvPython(API_DIR);

  if (!python) {
    console.error('venv 创建失败：apps/api/.venv 里没有解释器。');
    process.exit(1);
  }
}

step('安装后端依赖（editable + dev extras）');
run(python, ['-m', 'pip', 'install', '--quiet', '--upgrade', 'pip']);
run(python, ['-m', 'pip', 'install', '--quiet', '-e', `${API_DIR}[dev]`]);

step('安装前端依赖（bun workspaces）');
run(process.versions.bun ? process.execPath : 'bun', ['install']);

step('完成');
console.log('下一步：bun run dev');
