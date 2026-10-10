/**
 * Document-lifetime feature graph transactions, including Vite's CSS preload.
 * Entry: createFeatureModuleCache. Dependencies: caller-owned stable loaders.
 * Each loader retains one success or failure until document teardown. Failed
 * CSS must stay failed: Vite's private preload set skips a URL on a second
 * call, which would otherwise permit an unstyled module to execute.
 */
export function createFeatureModuleCache(): <T>(loader: () => Promise<T>) => Promise<T> {
  const flights = new WeakMap<() => Promise<unknown>, Promise<unknown>>();
  return <T>(loader: () => Promise<T>): Promise<T> => {
    let flight = flights.get(loader);
    if (!flight) {
      flight = Promise.resolve().then(loader);
      flights.set(loader, flight);
    }
    return flight as Promise<T>;
  };
}
