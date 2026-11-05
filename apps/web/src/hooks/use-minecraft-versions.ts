import type { MinecraftVersion } from 'src/lib/api-client';

import { useCallback, useEffect, useState } from 'react';

import { listMinecraftVersions } from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type MinecraftVersionsState =
  | { status: 'loading' }
  | { status: 'ready'; versions: MinecraftVersion[] }
  | { status: 'error'; message: string };

export type UseMinecraftVersionsReturn = {
  state: MinecraftVersionsState;
  refresh: () => void;
};

/**
 * 可注册的 Minecraft 版本（`GET /v1/minecraft-versions`，仅 admin）。
 *
 * 后端有 1 小时进程内缓存，所以打开注册对话框拉一次不贵；网络失败时后端会退回缓存，
 * 这里只处理「连缓存都没有」的错误。
 */
export function useMinecraftVersions(enabled = true): UseMinecraftVersionsReturn {
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<MinecraftVersionsState>(
    enabled ? { status: 'loading' } : { status: 'ready', versions: [] }
  );

  useEffect(() => {
    const controller = new AbortController();
    let active = true;

    if (!enabled) {
      setState({ status: 'ready', versions: [] });
    } else {
      setState({ status: 'loading' });

      listMinecraftVersions(controller.signal)
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
    }

    return () => {
      active = false;
      controller.abort();
    };
  }, [attempt, enabled]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { state, refresh };
}
