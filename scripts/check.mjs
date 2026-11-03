import { spawnSync } from 'node:child_process';
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join, resolve } from 'node:path';

import { venvBin } from './lib/python.mjs';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const API_DIR = join(ROOT, 'apps', 'api');
const WEB_DIR = join(ROOT, 'apps', 'web');
const BUN = process.versions.bun ? process.execPath : 'bun';

const TYPES_PATH = join(WEB_DIR, 'src', 'types', 'api.d.ts');

function runCommand({ command, args, cwd }) {
  const result = spawnSync(command, args, { cwd, stdio: 'inherit' });

  if (result.error) {
    console.error(result.error.message);
    return false;
  }

  return result.status === 0;
}

/** 契约漂移守卫：重新生成前端类型，若与仓库里的不一致就失败（并还原文件）。 */
function checkContractDrift() {
  const before = readFileSync(TYPES_PATH, 'utf8');
  const regenerated = runCommand({ command: BUN, args: ['run', 'gen:api'], cwd: WEB_DIR });

  if (!regenerated) return false;

  const after = readFileSync(TYPES_PATH, 'utf8');

  if (after === before) {
    console.log(`契约未漂移：${TYPES_PATH} 与 docs/openapi.yaml 一致`);
    return true;
  }

  writeFileSync(TYPES_PATH, before);
  console.error(
    '契约漂移：apps/web/src/types/api.d.ts 与 docs/openapi.yaml 不同步。\n' +
      '修复：bun run --cwd apps/web gen:api 并提交生成结果。'
  );

  return false;
}

const steps = [
  { label: 'web · typecheck', command: BUN, args: ['run', 'typecheck'], cwd: WEB_DIR },
  { label: 'web · lint', command: BUN, args: ['run', 'lint'], cwd: WEB_DIR },
  { label: 'web · 契约漂移守卫', run: checkContractDrift },
  { label: 'api · ruff', command: venvBin(API_DIR, 'ruff'), args: ['check', '.'], cwd: API_DIR },
  { label: 'api · mypy', command: venvBin(API_DIR, 'mypy'), args: [], cwd: API_DIR },
  { label: 'api · pytest', command: venvBin(API_DIR, 'pytest'), args: [], cwd: API_DIR },
];

let failed = 0;

for (const step of steps) {
  console.log(`\n\u001b[36m==> ${step.label}\u001b[0m`);

  if (step.run) {
    if (!step.run()) failed += 1;
    continue;
  }

  if (!step.command) {
    console.error('缺少可执行文件（apps/api/.venv 未就绪？先执行：bun run setup）');
    failed += 1;
    continue;
  }

  if (!runCommand(step)) failed += 1;
}

// 冒烟测试默认跳过：它会起真实端口，不适合每次都跑。
const withSmoke =
  process.argv.includes('--smoke') || ['1', 'true'].includes(process.env.MC_LIBRARIAN_SMOKE ?? '');

if (withSmoke) {
  console.log('\n\u001b[36m==> smoke（api + web 端到端）\u001b[0m');
  if (!runCommand({ command: BUN, args: [join(ROOT, 'scripts', 'smoke.mjs')], cwd: ROOT })) {
    failed += 1;
  }
} else {
  console.log('\n（跳过 smoke：加 `--smoke` 或 `MC_LIBRARIAN_SMOKE=1` 启用）');
}

console.log(failed === 0 ? '\n\u001b[32m全部通过\u001b[0m' : `\n\u001b[31m${failed} 项失败\u001b[0m`);

process.exit(failed === 0 ? 0 : 1);
