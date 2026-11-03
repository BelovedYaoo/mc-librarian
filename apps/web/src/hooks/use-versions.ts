import type { VersionInfo } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { listVersions } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type VersionsState =
  | { status: 'loading' }
  | { status: 'ready'; versions: VersionInfo[] }
  | { status: 'error'; message: string };

export type UseVersionsReturn = {
  state: VersionsState;
  refresh: () => void;
};

/** 版本列表：进入页面拉一次，注册成功后由调用方 `refresh()`。 */
export function useVersions(): UseVersionsReturn {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<VersionsState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    listVersions(controller.signal)
      .then((versions) => {
        if (active) setState({ status: 'ready', versions });
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
  }, [attempt]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { state, refresh };
}
