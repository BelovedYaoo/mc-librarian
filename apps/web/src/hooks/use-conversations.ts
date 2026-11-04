import type { ConversationSummary } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { listConversations } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type UseConversationsReturn = {
  conversations: ConversationSummary[];
  loading: boolean;
  error: string | null;
  refresh: () => void;
};

/** 我的历史会话列表：进入页面拉一次，每次问答收尾/删除后由调用方 `refresh()`。 */
export function useConversations(): UseConversationsReturn {
  const [attempt, setAttempt] = useState(0);
  const [conversations, setConversations] = useState<ConversationSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    setLoading(true);

    listConversations({ limit: 50 }, controller.signal)
      .then((next) => {
        if (!active) return;

        setConversations(next);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (!active || controller.signal.aborted) return;

        setError(cause instanceof Error ? cause.message : String(cause));
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { conversations, loading, error, refresh };
}
