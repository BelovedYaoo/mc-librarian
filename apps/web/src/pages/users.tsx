import { CONFIG } from 'src/global-config';

import { UsersView } from 'src/sections/users';

// ----------------------------------------------------------------------

const metadata = { title: `用户管理 - ${CONFIG.appName}` };

export default function UsersPage() {
  return (
    <>
      <title>{metadata.title}</title>

      <UsersView />
    </>
  );
}
