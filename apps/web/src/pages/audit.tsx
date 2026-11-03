import { CONFIG } from 'src/global-config';

import { AuditView } from 'src/sections/audit';

// ----------------------------------------------------------------------

const metadata = { title: `审计 - ${CONFIG.appName}` };

export default function AuditPage() {
  return (
    <>
      <title>{metadata.title}</title>

      <AuditView />
    </>
  );
}
