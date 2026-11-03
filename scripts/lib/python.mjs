import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join } from 'node:path';

/** 本仓库支持的 Python 主版本。 */
export const REQUIRED_MAJOR = 3;
export const REQUIRED_MINOR = 13;

const WIN = process.platform === 'win32';

/** Checkpoint: apps/api/.venv 里的解释器，未创建时返回 null。 */
export function venvPython(apiDir) {
  const candidates = WIN
    ? [join(apiDir, '.venv', 'Scripts', 'python.exe'), join(apiDir, '.venv', 'bin', 'python')]
    : [join(apiDir, '.venv', 'bin', 'python'), join(apiDir, '.venv', 'Scripts', 'python.exe')];

  return candidates.find((candidate) => existsSync(candidate)) ?? null;
}

/** Checkpoint: venv 里的可执行文件（ruff / mypy / pytest）。 */
export function venvBin(apiDir, name) {
  const dirs = WIN ? ['Scripts', 'bin'] : ['bin', 'Scripts'];
  const suffixes = WIN ? ['.exe', ''] : ['', '.exe'];

  for (const dir of dirs) {
    for (const suffix of suffixes) {
      const candidate = join(apiDir, '.venv', dir, `${name}${suffix}`);
      if (existsSync(candidate)) return candidate;
    }
  }

  return null;
}

function isPython313(command, prefixArgs) {
  const result = spawnSync(command, [...prefixArgs, '--version'], { encoding: 'utf8' });

  if (result.error || result.status !== 0) return false;

  const version = `${result.stdout ?? ''}${result.stderr ?? ''}`;
  const matched = /Python\s+(\d+)\.(\d+)/.exec(version);

  return matched ? Number(matched[1]) === REQUIRED_MAJOR && Number(matched[2]) === REQUIRED_MINOR : false;
}

/** Checkpoint: 找得到 3.13 解释器才能建 venv。`MC_LIBRARIAN_PYTHON` 优先。 */
export function findSystemPython() {
  const explicit = process.env.MC_LIBRARIAN_PYTHON;

  const candidates = explicit
    ? [{ command: explicit, args: [] }]
    : [
        { command: 'python3.13', args: [] },
        { command: 'py', args: [`-${REQUIRED_MAJOR}.${REQUIRED_MINOR}`] },
        { command: 'python3', args: [] },
        { command: 'python', args: [] },
      ];

  for (const { command, args } of candidates) {
    if (isPython313(command, args)) return { command, args };
  }

  return null;
}
