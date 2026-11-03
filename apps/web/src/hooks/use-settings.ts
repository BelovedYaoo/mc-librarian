import type { AdminSettings } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { getSettings } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type AdminSettingsState =
  | { status: 'loading' }
  | { status: 'ready'; settings: AdminSettings | null }
  | { status: 'error'; message: string };

export type UseAdminSettingsReturn = {
  state: AdminSettingsState;
  refresh: () => void;
};

/**
 * 实例设置（`GET /v1/admin/settings`，仅 admin）。
 *
 * `enabled=false` 时不发请求、直接给 `ready + null`（非 admin 打开版本页会 403，
 * 没必要为了一个用不上的标志发一次注定失败的请求）。
 */
export function useAdminSettings(enabled = true): UseAdminSettingsReturn {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<AdminSettingsState>(
    enabled ? { status: 'loading' } : { status: 'ready', settings: null }
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    if (!enabled) {
      setState({ status: 'ready', settings: null });
    } else {
      setState({ status: 'loading' });

      getSettings(controller.signal)
        .then((settings) => {
          if (active) setState({ status: 'ready', settings });
        })
        .catch((error: unknown) => {
          if (!active || controller.signal.aborted) return;

          setState({
            status: 'error',
            message: error instanceof Error ? error.message : String(error),
          });
        });
    }

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, enabled]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { state, refresh };
}
