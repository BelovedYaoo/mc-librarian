import { CONFIG } from 'src/global-config';

import { AuthLayout } from 'src/layouts/auth';

import { LoginView } from 'src/sections/auth';

// ----------------------------------------------------------------------

const metadata = { title: `登录 - ${CONFIG.appName}` };

export default function LoginPage() {
  return (
    <>
      <title>{metadata.title}</title>

      <AuthLayout title="登录" subtitle="用管理员或普通用户账号登录后台">
        <LoginView />
      </AuthLayout>
    </>
  );
}
