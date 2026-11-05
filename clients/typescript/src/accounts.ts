/** API-key and webhook routes. Request authority and transport belong to the client. */
import type { Tofu } from './client.js';
import type { ApiKey } from './types.js';

export class KeysAPI {
  constructor(private c: Tofu) {}

  whoami(): Promise<Record<string, unknown>> {
    return this.c._json('GET', '/api/v1/keys/whoami');
  }
  list(): Promise<{ keys: ApiKey[] }> {
    return this.c._json('GET', '/api/v1/keys');
  }
  create(params: { name: string; scopes: string[]; rate_limit_rpm?: number; rate_limit_tpd?: number; admin?: boolean }): Promise<{ key: ApiKey; token: string }> {
    return this.c._json('POST', '/api/v1/keys', { json: params });
  }
  revoke(keyId: string): Promise<{ revoked: string }> {
    return this.c._json('DELETE', `/api/v1/keys/${encodeURIComponent(keyId)}`);
  }
}

export class WebhooksAPI {
  constructor(private c: Tofu) {}

  list(): Promise<{ subs: unknown[] }> {
    return this.c._json('GET', '/api/v1/webhooks');
  }
  subscribe(params: { url: string; channel?: string; event_types?: string[]; task_id?: string }): Promise<{ subscription: { id: string; secret: string; url: string } }> {
    return this.c._json('POST', '/api/v1/webhooks', {
      json: { task_id: '*', ...params },
    });
  }
  unsubscribe(subId: string): Promise<{ deleted: string }> {
    return this.c._json('DELETE', `/api/v1/webhooks/${encodeURIComponent(subId)}`);
  }
}
