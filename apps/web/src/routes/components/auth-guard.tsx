import { Navigate } from 'react-router';

import { useAuth } from 'src/auth';
import { SplashScreen } from 'src/components/loading-screen';
import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

type GuardProps = {
  children: React.ReactNode;
};

/**
 * 受保护路由：会话状态未知时先转圈，未初始化去 setup，未登录去登录页。
 * 只是体验层；真正的判定在每个后端端点里。
 */
export function RequireAuth({ children }: GuardProps) {
  const { status, user, setupRequired } = useAuth();

  if (status === 'loading') return <SplashScreen />;

  if (setupRequired) return <Navigate to={paths.setup} replace />;

  if (!user) return <Navigate to={paths.login} replace />;

  return <>{children}</>;
}

/** 已登录就别再看登录页；还没初始化则先去 setup。 */
export function RequireGuest({ children }: GuardProps) {
  const { status, user, setupRequired } = useAuth();

  if (status === 'loading') return <SplashScreen />;

  if (setupRequired) return <Navigate to={paths.setup} replace />;

  if (user) return <Navigate to={paths.home} replace />;

  return <>{children}</>;
}

/** setup 页只在控制面还没有用户时可达：初始化完成后回到首页或登录页。 */
export function RequireSetup({ children }: GuardProps) {
  const { status, user, setupRequired } = useAuth();

  if (status === 'loading') return <SplashScreen />;

  if (!setupRequired) return <Navigate to={user ? paths.home : paths.login} replace />;

  return <>{children}</>;
}
