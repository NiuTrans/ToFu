/** Polls the admin approval projection during restart preparation.
 * Entry: watchRestartPreparation; ports: typed endpoint adapter and renderer.
 * Owns one bounded request/timer, stopped when preparation settles or retries.
 */
import schema from '../../../../contracts/restart_preparation_v1.json';

export interface RestartPreparation {
  phase: string;
  elapsedSeconds: number;
  updatedAt: number;
  remainingSeconds?: number;

  recovering?: boolean;
}

export function watchRestartPreparation(
  read: () => Promise<unknown>,
  render: (progress: RestartPreparation) => void,
): () => void {
  let active = true;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let lastUpdate = 0;
  async function poll(): Promise<void> {
    try {
      const response = await read() as { record?: { preparation?: RestartPreparation } } | null;
      const progress = response?.record?.preparation;
      if (active && progress && schema.properties.phase.enum.includes(progress.phase)
          && Number.isFinite(progress.updatedAt) && progress.updatedAt >= lastUpdate
          && Number.isFinite(progress.elapsedSeconds) && progress.elapsedSeconds >= 0) {
        lastUpdate = progress.updatedAt;
        render(progress);
      }
    } catch {
      // A failed observation never declares a completed or failed restart.
      // The executable POST retains failure and acceptance authority.
    } finally {
      if (active) timer = setTimeout(() => { void poll(); }, 2000);
    }
  }
  void poll();
  return () => {
    active = false;
    if (timer !== undefined) clearTimeout(timer);
  };
}
