/**
 * 后端时间是 UTC 的 ISO 串（`2026-11-05T10:25:12Z`）；统一按本地时区展示。
 *
 * 之前任务页 / 版本页 / 令牌页 / 审计页各写了一份，签名还不一致（有的接 `string`、
 * 有的接 `string | undefined`，空值处理也不同）——同一个时间在不同页面可能显示得不一样。
 */
export function formatTime(value?: string | null, fallback = '—'): string {
  if (!value) return fallback;

  const date = new Date(value);

  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN');
}
