import type { Job, JobLog } from 'src/lib/api-client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { getJobLogs, listJobs } from 'src/lib/api-client';

// ----------------------------------------------------------------------

/** 列表刷新间隔：任务阶段推进很快，2 秒足够让进度条看起来是「实时」的。 */
const JOBS_POLL_MS = 2000;
const LOGS_POLL_MS = 1500;

export type UseJobsReturn = {
  jobs: Job[];
  error: string | null;
  refresh: () => void;
};

/** 任务列表轮询。`refresh()` 用于取消/新建后立刻拉一次。 */
export function useJobs(pollMs: number = JOBS_POLL_MS): UseJobsReturn {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;

    const load = () =>
      listJobs({ limit: 50 })
        .then((next) => {
          if (!active) return;

          setJobs(next);
          setError(null);
        })
        .catch((cause: unknown) => {
          if (!active) return;

          setError(cause instanceof Error ? cause.message : String(cause));
        });

    void load();
    const timer = setInterval(() => void load(), pollMs);

    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [attempt, pollMs]);

  const refresh = useCallback(() => setAttempt((current) => current + 1), []);

  return { jobs, error, refresh };
}

export type UseJobLogsReturn = {
  lines: JobLog[];
  error: string | null;
};

/** 日志面板：按 `after=<已见最大 id>` 增量轮询，切换任务时清空重来。 */
export function useJobLogs(jobId: string | null, pollMs: number = LOGS_POLL_MS): UseJobLogsReturn {
  const [lines, setLines] = useState<JobLog[]>([]);
  const [error, setError] = useState<string | null>(null);
  const lastId = useRef(0);

  useEffect(() => {
    lastId.current = 0;
    setLines([]);
    setError(null);

    if (!jobId) return undefined;

    let active = true;

    const load = () =>
      getJobLogs(jobId, { after: lastId.current })
        .then((next) => {
          if (!active || next.length === 0) return;

          lastId.current = next[next.length - 1].id;
          setLines((current) => [...current, ...next]);
          setError(null);
        })
        .catch((cause: unknown) => {
          if (!active) return;

          setError(cause instanceof Error ? cause.message : String(cause));
        });

    void load();
    const timer = setInterval(() => void load(), pollMs);

    return () => {
      active = false;
      clearInterval(timer);
    };
  }, [jobId, pollMs]);

  return { lines, error };
}
