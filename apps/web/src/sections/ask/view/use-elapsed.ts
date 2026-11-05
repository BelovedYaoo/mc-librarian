/** 实时计时：从 `startedAt` 起每 100ms 自增，`startedAt === null` 时归零并停表。 */

import { useEffect, useState } from 'react';

export function useElapsed(startedAt: number | null): number {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    if (startedAt === null) {
      setElapsed(0);

      return undefined;
    }

    // 先立刻算一次：`startedAt` 可能是几秒前设的（组件挂载晚于请求开始）。
    setElapsed(Date.now() - startedAt);

    const timer = window.setInterval(() => setElapsed(Date.now() - startedAt), 100);

    return () => window.clearInterval(timer);
  }, [startedAt]);

  return elapsed;
}
