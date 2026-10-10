/**
 * Responsibility: drain independent asynchronous work with an explicit
 * in-flight ceiling while isolating per-item failures.
 * Entry point: runWithConcurrency. Dependencies: none; work is injected.
 */

export const DEFAULT_ASYNC_POOL_CONCURRENCY = 4;

export type AsyncPoolWorker<Item> = (
  item: Item,
  index: number,
) => unknown | PromiseLike<unknown>;

export interface AsyncPoolResult {
  readonly completed: number;
  readonly errors: readonly unknown[];
}

function concurrencyLimit(limit: unknown): number {
  return typeof limit === 'number' && limit >= 1
    ? Math.floor(limit)
    : DEFAULT_ASYNC_POOL_CONCURRENCY;
}

/**
 * Visit every item exactly once with at most `limit` workers in flight.
 * Worker failures are returned in settlement order and never abort the pool.
 */
export function runWithConcurrency<Item>(
  items: readonly Item[] | unknown,
  worker: AsyncPoolWorker<Item>,
  limit: number = DEFAULT_ASYNC_POOL_CONCURRENCY,
): Promise<AsyncPoolResult> {
  const list: readonly Item[] = Array.isArray(items)
    ? items as readonly Item[] : [];
  const cap = concurrencyLimit(limit);

  return new Promise((resolve) => {
    if (list.length === 0) {
      resolve({ completed: 0, errors: [] });
      return;
    }

    let nextIndex = 0;
    let activeWorkers = 0;
    let completed = 0;
    const errors: unknown[] = [];
    let settled = false;

    const pump = (): void => {
      if (settled) return;
      if (completed >= list.length) {
        settled = true;
        resolve({ completed, errors });
        return;
      }
      while (activeWorkers < cap && nextIndex < list.length) {
        const index = nextIndex;
        const item = list[index];
        nextIndex += 1;
        activeWorkers += 1;
        Promise.resolve()
          .then(() => worker(item, index))
          .catch((error: unknown) => {
            errors.push(error);
          })
          .then(() => {
            activeWorkers -= 1;
            completed += 1;
            pump();
          });
      }
    };

    pump();
  });
}
