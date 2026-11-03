import type { HealthResponse } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { getHealth } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type HealthState =
  | { status: 'loading' }
  | { status: 'ok'; data: HealthResponse; checkedAt: Date }
  | { status: 'error'; message: string; checkedAt: Date };

export type UseHealthReturn = {
  state: HealthState;
  refresh: () => void;
};

export function useHealth(): UseHealthReturn {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<HealthState>({ status: 'loading' });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setState({ status: 'loading' });

    getHealth(controller.signal)
      .then((data) => {
        if (active) setState({ status: 'ok', data, checkedAt: new Date() });
      })
      .catch((error: unknown) => {
        if (!active || controller.signal.aborted) return;

        setState({
          status: 'error',
          message: error instanceof Error ? error.message : String(error),
          checkedAt: new Date(),
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
