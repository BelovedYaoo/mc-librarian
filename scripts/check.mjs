import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { venvBin } from './lib/python.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const API_DIR = join(ROOT, 'apps', 'api');
const WEB_DIR = join(ROOT, 'apps', 'web');
const BUN = process.versions.bun ? process.execPath : 'bun';

const steps = [
  { label: 'web · typecheck', command: BUN, args: ['run', 'typecheck'], cwd: WEB_DIR },
  { label: 'web · lint', command: BUN, args: ['run', 'lint'], cwd: WEB_DIR },
  { label: 'api · ruff', command: venvBin(API_DIR, 'ruff'), args: ['check', '.'], cwd: API_DIR },
  { label: 'api · mypy', command: venvBin(API_DIR, 'mypy'), args: [], cwd: API_DIR },
  { label: 'api · pytest', command: venvBin(API_DIR, 'pytest'), args: [], cwd: API_DIR },
];

let failed = 0;

for (const { label, command, args, cwd } of steps) {
  console.log(`\n\u001b[36m==> ${label}\u001b[0m`);

  if (!command) {
    console.error('缺少可执行文件（apps/api/.venv 未就绪？先执行：bun run setup）');
    failed += 1;
    continue;
  }

  const result = spawnSync(command, args, { cwd, stdio: 'inherit' });

  if (result.error) {
    console.error(result.error.message);
    failed += 1;
  } else if (result.status !== 0) {
    failed += 1;
  }
}

console.log(failed === 0 ? '\n\u001b[32m全部通过\u001b[0m' : `\n\u001b[31m${failed} 项失败\u001b[0m`);

process.exit(failed === 0 ? 0 : 1);
