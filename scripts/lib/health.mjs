/** 轮询健康探针直到 `status === 'ok'`，用于 dev / smoke 的就绪判定。 */
export async function waitForHealth(url, { timeoutMs = 60_000, intervalMs = 500 } = {}) {
  const deadline = Date.now() + timeoutMs;
  let lastError = '尚未发出请求';

  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, {
        headers: { Accept: 'application/json' },
        signal: AbortSignal.timeout(3_000),
      });

      if (response.ok) {
        const payload = await response.json();

        if (payload?.status === 'ok') return payload;

        lastError = `响应不是 status=ok：${JSON.stringify(payload)}`;
      } else {
        lastError = `HTTP ${response.status}`;
      }
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }

    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  throw new Error(`${url} 在 ${timeoutMs}ms 内未就绪（最后一次：${lastError}）`);
}
