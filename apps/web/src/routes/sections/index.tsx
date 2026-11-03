import type { RouteObject } from 'react-router';

import { Suspense, lazy } from 'react';

import { MainLayout } from 'src/layouts/main';

import { SplashScreen } from 'src/components/loading-screen';

import { RequireAuth, RequireGuest, RequireSetup } from 'src/routes/components';
import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

const HomePage = lazy(() => import('src/pages/home'));
const LoginPage = lazy(() => import('src/pages/login'));
const SetupPage = lazy(() => import('src/pages/setup'));
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
