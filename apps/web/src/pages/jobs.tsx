import { CONFIG } from 'src/global-config';

import { JobsView } from 'src/sections/jobs';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 任务` };

export default function JobsPage() {
  return (
    <>
      <title>{metadata.title}</title>
      <JobsView />
    </>
  );
}
