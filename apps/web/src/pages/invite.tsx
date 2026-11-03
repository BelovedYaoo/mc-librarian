import { CONFIG } from 'src/global-config';

import { AuthLayout } from 'src/layouts/auth';

import { InviteView } from 'src/sections/invite';

// ----------------------------------------------------------------------

const metadata = { title: `接受邀请 - ${CONFIG.appName}` };

export default function InvitePage() {
  return (
    <>
      <title>{metadata.title}</title>

      <AuthLayout title="接受邀请" subtitle="设置你自己的密码，完成后直接进入后台">
        <InviteView />
      </AuthLayout>
    </>
  );
}
