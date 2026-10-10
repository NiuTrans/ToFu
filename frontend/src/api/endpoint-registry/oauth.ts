/** oauth endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, JsonObject } from './contracts';
export function createOauthEndpoints(ports: EndpointPorts) {
  const { request, get, post } = ports;
  const oauth = {
    status:      ()                 => get('/api/v4/oauth/status', { onError: 'null' }),
    loginPost:   (provider: string, preferConsole: boolean) =>
      post<Response | null>('/api/oauth/login',
           preferConsole ? { provider, prefer_console: true } : { provider },
           { onError: 'null', parse: 'response' }),
    loginGet:    (provider: string, preferConsole: boolean) =>
      request<Response | null>('/api/oauth/login',
              { method: 'GET',
                query: preferConsole ? { provider, prefer_console: '1' } : { provider },
                parse: 'response', onError: 'null' }),
    logoutPost:  (provider: string)         => post<Response | null>('/api/oauth/logout', { provider }, { onError: 'null', parse: 'response' }),
    logoutGet:   (provider: string)         =>
      request<Response | null>('/api/oauth/logout',
              { method: 'GET', query: { provider }, parse: 'response', onError: 'null' }),
    callbackPost: (body: JsonObject)            => post<Response | null>('/api/oauth/callback', body, { onError: 'null', parse: 'response' }),
    storeToken:  (provider: string, token: string)  => post<Response | null>('/api/oauth/store-token', { provider, token }, { onError: 'null', parse: 'response' }),
    callbackGet:  (queryString: string)     =>
      // queryString is a pre-encoded "k=v&k2=v2" string — we hand it off raw
      // because the legacy code did the same. Future cleanup can switch to
      // a structured object.
      request<Response | null>('/api/oauth/callback' + (queryString ? '?' + queryString : ''),
              { method: 'GET', parse: 'response', onError: 'null' }),
    deviceLoginPost: (provider: string)     => post<Response | null>('/api/v4/oauth/device-login', { provider }, { onError: 'null', parse: 'response' }),
    deviceLoginGet:  (provider: string)     =>
      request<Response | null>('/api/v4/oauth/device-login',
              { method: 'GET', query: { provider }, parse: 'response', onError: 'null' }),
    resetOfferConsume: ()           => post<Response | null>('/api/v4/oauth/reset-offer/consume', {}, { onError: 'null', parse: 'response' }),
    egressAgentGet: ()              => get('/api/v4/oauth/egress-agent', { onError: 'null' }),
    // CatPaw (Credit Hub) one-click local import — not OAuth; the CLI's
    // fixed loopback config is the only credential issuer.
    catpawImportStatus: ()          => get('/api/v4/providers/catpaw/local-import', { onError: 'null' }),
    catpawImportApply: ()           => post<Response | null>('/api/v4/providers/catpaw/local-import', {}, { onError: 'null', parse: 'response' }),
    catpawImportLogin: ()           => post('/api/v4/providers/catpaw/local-import/login', {}, { onError: 'null' }),
    egressAgentSet: (agentId: string)       => post<Response | null>('/api/v4/oauth/egress-agent', { agent_id: agentId }, { onError: 'null', parse: 'response' }),
  };
  return oauth;
}
