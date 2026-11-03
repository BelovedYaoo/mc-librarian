import type { UsageGroupBy, UsageReport } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { getUsage } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type UsageState =
  | { status: 'loading' }
  | { status: 'ready'; report: UsageReport }
  | { status: 'error'; message: string };

export type UsageRange = {
  from: string;
  to: string;
  groupBy: UsageGroupBy;
};

export type UseUsageReturn = {
  state: UsageState;
  refresh: () => void;
};

/**
 * 用量报表（`GET /v1/admin/usage`，仅 admin）。
 *
 * 区间或 `group_by` 变化即重新拉取；页面直接渲染接口返回的 `totals`/`buckets`，
 * 不在前端再算一遍（验收第 2 条：聚合接口与页面数字一致）。
 */
export function useUsage(range: UsageRange): UseUsageReturn {
  const { from, to, groupBy } = range;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<UsageState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setState({ status: 'loading' });

    getUsage({ from, to, group_by: groupBy }, controller.signal)
      .then((report) => {
        if (active) setState({ status: 'ready', report });
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return;

        setState({
          status: 'error',
          message: error instanceof Error ? error.message : String(error),
        });
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [from, to, groupBy, attempt]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { state, refresh };
}
