import { CONFIG } from 'src/global-config';

import { BrowseView } from 'src/sections/browse';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 检索` };

export default function BrowsePage() {
  return (
    <>
      <title>{metadata.title}</title>
      <BrowseView />
    </>
  );
}
