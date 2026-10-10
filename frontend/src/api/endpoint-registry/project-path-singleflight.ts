/** Registry-scoped Project-path write sharing. Bound pending identities, preserve selection and clone consumable responses. */
import type { JsonRequest } from './contracts';
export function createProjectPathWrite(request: JsonRequest) {
  const _PROJECT_PATH_SINGLEFLIGHT_CAPACITY = 16;
  const _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS = 16 * 1024;
  const _PROJECT_RECENT_PATH_BATCH_LIMIT = 32;
  const inflight = new Map<string, Promise<Response | null>>();
  function _cloneProjectPathResponse(response: Response | null) {
    return response && typeof response.clone === 'function'
      ? response.clone() : response;
  }
  function setPaths(folders: readonly unknown[], readOnlyPaths: readonly unknown[] = [], recentPaths: readonly unknown[] = []) {
    const body: { paths: readonly unknown[]; readOnlyPaths: readonly unknown[]; recentPaths?: readonly unknown[] } = { paths: folders, readOnlyPaths: readOnlyPaths || [] };
    if (Array.isArray(recentPaths) && recentPaths.length) {
      // Keep project selection authoritative even beyond the reconstructible
      // recent-history batch cap. The primary path is always index zero.
      body.recentPaths = recentPaths.slice(0, _PROJECT_RECENT_PATH_BATCH_LIMIT);
    }
    const key = JSON.stringify([
      body.paths, body.readOnlyPaths, body.recentPaths || [],
    ]);
    if (key.length <= _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS) {
      const existing = inflight.get(key);
      if (existing) return existing.then(_cloneProjectPathResponse);
    }
    const pending = request<Response | null>('/api/v4/project/paths', {
      method: 'PUT', json: body, parse: 'response',
    });
    if (key.length <= _PROJECT_PATH_SINGLEFLIGHT_KEY_CHARS
        && inflight.size < _PROJECT_PATH_SINGLEFLIGHT_CAPACITY) {
      inflight.set(key, pending);
      const clear = () => {
        if (inflight.get(key) === pending) {
          inflight.delete(key);
        }
      };
      pending.then(clear, clear);
    }
    return pending.then(_cloneProjectPathResponse);
  }
  return setPaths;
}
