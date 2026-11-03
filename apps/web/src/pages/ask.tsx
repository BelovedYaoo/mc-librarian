import { CONFIG } from 'src/global-config';

import { AskView } from 'src/sections/ask';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 问答` };

export default function AskPage() {
  return (
    <>
      <title>{metadata.title}</title>
      <AskView />
    </>
  );
}
