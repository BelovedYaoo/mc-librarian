import type { RouteObject } from 'react-router';

import { Suspense, lazy } from 'react';

import { MainLayout } from 'src/layouts/main';

import { SplashScreen } from 'src/components/loading-screen';

import { RequireAdmin, RequireAuth, RequireGuest, RequireSetup } from 'src/routes/components';
import { inviteRoute, paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

const HomePage = lazy(() => import('src/pages/home'));
const SettingsPage = lazy(() => import('src/pages/settings'));
const LoginPage = lazy(() => import('src/pages/login'));
const SetupPage = lazy(() => import('src/pages/setup'));
const UsersPage = lazy(() => import('src/pages/users'));
const InvitePage = lazy(() => import('src/pages/invite'));
const Page404 = lazy(() => import('src/pages/error/404'));

export const routesSection: RouteObject[] = [
  // 首次启动引导：只在控制面还没有用户时可达
  {
    path: paths.setup,
    element: (
      <RequireSetup>
        <Suspense fallback={<SplashScreen />}>
          <SetupPage />
        </Suspense>
      </RequireSetup>
    ),
  },

  // 未登录可访问；已登录会被送回首页
  {
    path: paths.login,
    element: (
      <RequireGuest>
        <Suspense fallback={<SplashScreen />}>
          <LoginPage />
        </Suspense>
      </RequireGuest>
    ),
  },

  // 接受邀请：被邀请人还没有账号，必须公开可达（也不该被「已登录」守卫弹走）
  {
    path: inviteRoute,
    element: (
      <Suspense fallback={<SplashScreen />}>
        <InvitePage />
      </Suspense>
    ),
  },

  // 受保护的后台外壳
  {
    path: paths.home,
    element: (
      <RequireAuth>
        <Suspense fallback={<SplashScreen />}>
          <MainLayout>
            <HomePage />
          </MainLayout>
        </Suspense>
      </RequireAuth>
    ),
  },

  // 账号设置（个人访问令牌）
  {
    path: paths.settings,
    element: (
      <RequireAuth>
        <Suspense fallback={<SplashScreen />}>
          <MainLayout>
            <SettingsPage />
          </MainLayout>
        </Suspense>
      </RequireAuth>
    ),
  },

  // 用户管理：仅 admin（后端每个端点仍会独立校验）
  {
    path: paths.users,
    element: (
      <RequireAdmin>
        <Suspense fallback={<SplashScreen />}>
          <MainLayout>
            <UsersPage />
          </MainLayout>
        </Suspense>
      </RequireAdmin>
    ),
  },

  // No match（与受保护路由一致：未登录先去登录页）
  {
    path: '*',
    element: (
      <RequireAuth>
        <Page404 />
      </RequireAuth>
    ),
  },
];
