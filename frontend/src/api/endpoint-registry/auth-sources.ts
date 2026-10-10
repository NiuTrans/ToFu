/** authSources endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createAuthSourcesEndpoints(ports: EndpointPorts) {
  const { get, post, del } = ports;
  const authSources = {
    list:   ()              => get('/api/v4/auth-sources', { onError: 'null' }),
    upsert: (body: JsonObject)          => post('/api/v4/auth-sources', body),
    toggle: (domain: string, on: boolean)    => post(`/api/v4/auth-sources/${encodeURIComponent(domain)}/toggle`, { enabled: !!on }, { onError: 'null' }),
    remove: (domain: string)        => del<Response | null>(`/api/v4/auth-sources/${encodeURIComponent(domain)}`, { parse: 'response', onError: 'null' }),
    // Interactive headful login — long-running; no client timeout.
    login:  (domain: string, timeout: number) => post(`/api/v4/auth-sources/${encodeURIComponent(domain)}/login`, { timeout: timeout || 180 }, { timeout: 0 }),
    // Live-session probe: is the user logged into the site in THEIR browser
    // right now (bridge get_cookies; jar never leaves the browser)?
    liveSession: (domain: string, refresh: boolean) => get(`/api/v4/auth-sources/${encodeURIComponent(domain)}/live-session${refresh ? '?refresh=1' : ''}`, { onError: 'null' }),
  };
  return authSources;
}
