import { CONFIG } from 'src/global-config';

import { UsageView } from 'src/sections/usage';

// ----------------------------------------------------------------------

const metadata = { title: `用量 - ${CONFIG.appName}` };

export default function UsagePage() {
  return (
    <>
      <title>{metadata.title}</title>

      <UsageView />
    </>
  );
}
