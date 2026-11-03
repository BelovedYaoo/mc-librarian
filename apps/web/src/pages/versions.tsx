import { CONFIG } from 'src/global-config';

import { VersionsView } from 'src/sections/versions';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 版本` };

export default function VersionsPage() {
  return (
    <>
      <title>{metadata.title}</title>
      <VersionsView />
    </>
  );
}
