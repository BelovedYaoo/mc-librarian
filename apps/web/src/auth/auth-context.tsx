import type { User } from 'src/lib/api-client';

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useLocation, useNavigate } from 'react-router';

import {
  ApiError,
  createFirstAdmin,
  getMe,
  getSetupStatus,
  login,
  logout,
  setUnauthorizedHandler,
} from 'src/lib/api-client';

import { paths } from 'src/routes/paths';

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

const SIGNED_OUT: AuthState = { status: 'ready', user: null, setupRequired: false };

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
 *
 * 另外两件事：
 * - 向 `api-client` 注册统一的 401 回调：任何请求碰到「会话已失效」都清空上下文并回登录页；
 * - 路由切换时重新校验一次 `me`，服务端踢人或会话过期后不会停在看似可用的页面上。
 */
export function AuthProvider({ children }: AuthProviderProps) {
  const navigate = useNavigate();
  const { pathname } = useLocation();

  const [state, setState] = useState<AuthState>({
    status: 'loading',
    user: null,
    setupRequired: false,
  });

  // 给 401 回调读最新状态用：回调注册一次，不能闭包到首次 render 的 state。
  const stateRef = useRef(state);

  useEffect(() => {
    stateRef.current = state;
  }, [state]);

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
      setState(SIGNED_OUT);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  /**
   * 路由切换时重新校验会话。首次进入由 `refresh` 负责，这里跳过第一帧避免重复请求；
   * 未初始化（setupRequired）时没有会话可校验，直接跳过。
   */
  const revalidate = useCallback(async () => {
    if (stateRef.current.setupRequired) return;

    try {
      const user = await getMe();
      setState({ status: 'ready', user, setupRequired: false });
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setState(SIGNED_OUT);
        return;
      }
      // 后端瞬时不可达时保持现状，不把已登录用户误踢出去。
    }
  }, []);

  const previousPathname = useRef(pathname);

  useEffect(() => {
    if (previousPathname.current === pathname) return;

    previousPathname.current = pathname;
    void revalidate();
  }, [pathname, revalidate]);

  /** 任何请求拿到 401：清空会话；之前确实有会话的话再回登录页（否则路由守卫会自己处理）。 */
  useEffect(() => {
    setUnauthorizedHandler(() => {
      const hadSession = stateRef.current.user !== null;

      setState(SIGNED_OUT);

      if (hadSession) navigate(paths.login, { replace: true });
    });

    return () => setUnauthorizedHandler(null);
  }, [navigate]);

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
      setState(SIGNED_OUT);
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
