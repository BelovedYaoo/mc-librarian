import { spawnSync } from 'node:child_process';

/**
 * 结束子进程（含其子孙）。
 *
 * Windows 上 `child.kill()` 只杀直接子进程，`bun run dev → vite` 这类包装会留下孤儿进程继续占端口，
 * 所以用 taskkill /T 做进程树清理。
 */
export function killTree(child) {
  if (!child || child.exitCode !== null || !child.pid) return;

  if (process.platform === 'win32') {
    spawnSync('taskkill', ['/pid', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    return;
  }

  child.kill('SIGTERM');
}
