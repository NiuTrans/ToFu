/** sitePacks endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createSitePacksEndpoints(ports: EndpointPorts) {
  const { get, post, del } = ports;
  const sitePacks = {
    list:    ()        => get('/api/v4/site-packs', { onError: 'null' }),
    install: (body: JsonObject)    => post('/api/v4/site-packs', body),
    remove:  (packId: string)  => del<Response | null>(`/api/v4/site-packs/${encodeURIComponent(packId)}`, { parse: 'response', onError: 'null' }),
  };
  return sitePacks;
}
