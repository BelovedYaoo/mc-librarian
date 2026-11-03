import { CONFIG } from 'src/global-config';

import { AuthLayout } from 'src/layouts/auth';

import { SetupView } from 'src/sections/auth';

// ----------------------------------------------------------------------

const metadata = { title: `初始化 - ${CONFIG.appName}` };

export default function SetupPage() {
  return (
    <>
      <title>{metadata.title}</title>

      <AuthLayout title="初始化管理员" subtitle="控制面还没有任何账号，先创建唯一管理员">
        <SetupView />
      </AuthLayout>
    </>
  );
}
