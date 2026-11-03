import type { User } from 'src/lib/api-client';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import {
  ApiError,
  createFirstAdmin,
  getMe,
  getSetupStatus,
  login,
  logout,
} from 'src/lib/api-client';

// ----------------------------------------------------------------------

export type AuthStatus = 'loading' | 'ready';

export type AuthState = {
  status: AuthStatus;
  user: User | null;
  /** 控制面还没有任何用户：前端必须先把人送到 `/setup`。 */
  setupRequired: boolean;
};

export type AuthContextValue = AuthState & {
  refresh: () => Promise<void>;
  signIn: (username: string, password: string) => Promise<User>;
  completeSetup: (username: string, password: string) => Promise<User>;
  signOut: () => Promise<void>;
};

const AuthContext = createContext<AuthContextValue | null>(null);

// ----------------------------------------------------------------------

export type AuthProviderProps = {
  children: React.ReactNode;
};

/**
 * 会话状态的唯一来源。
 *
 * 启动时先问 `GET /v1/setup`（未登录也能调）：还需要初始化就直接把人导向 `/setup`，
 * 否则用 `GET /v1/auth/me` 试读当前用户——cookie 有效就是已登录，401 就是未登录。
 * 会话在服务端，前端不存 token，刷新页面不会掉线。
 */
export function AuthProvider({ children }: AuthProviderProps) {
  const [state, setState] = useState<AuthState>({
    status: 'loading',
    user: null,
    setupRequired: false,
  });

  const refresh = useCallback(async () => {
    try {
      const setup = await getSetupStatus();

      if (setup.required) {
        setState({ status: 'ready', user: null, setupRequired: true });
        return;
      }

      try {
        const user = await getMe();
        setState({ status: 'ready', user, setupRequired: false });
      } catch (error) {
        if (!(error instanceof ApiError) || error.status !== 401) throw error;

        setState({ status: 'ready', user: null, setupRequired: false });
      }
    } catch {
      // 后端不可达也按「未登录」处理：登录页会把错误显示出来，不至于白屏。
      setState({ status: 'ready', user: null, setupRequired: false });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const signIn = useCallback(async (username: string, password: string) => {
    const user = await login({ username, password });

    setState({ status: 'ready', user, setupRequired: false });

    return user;
  }, []);

  const completeSetup = useCallback(async (username: string, password: string) => {
    const user = await createFirstAdmin({ username, password });

    setState({ status: 'ready', user, setupRequired: false });

    return user;
  }, []);

  const signOut = useCallback(async () => {
    try {
      await logout();
    } catch {
      // 会话可能已经过期（401），本地照样清干净。
    } finally {
      setState({ status: 'ready', user: null, setupRequired: false });
    }
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ ...state, refresh, signIn, completeSetup, signOut }),
    [state, refresh, signIn, completeSetup, signOut]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// ----------------------------------------------------------------------

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);

  if (!context) {
    throw new Error('useAuth 必须在 AuthProvider 内使用');
  }

  return context;
}
