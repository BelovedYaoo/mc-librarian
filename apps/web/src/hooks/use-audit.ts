import type { AuditEntry } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { listAudit } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type UseAuditReturn = {
  entries: AuditEntry[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
};

/** 审计流水：`actor` 非空时按用户名精确筛选，筛选条件变化即重新拉取。 */
export function useAudit(actor: string): UseAuditReturn {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setLoading(true);

    listAudit({ actor: actor.trim() || undefined }, controller.signal)
      .then((next) => {
        if (!active) return;

        setEntries(next);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!active) return;

        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [actor, attempt]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { entries, loading, error, refresh };
}
