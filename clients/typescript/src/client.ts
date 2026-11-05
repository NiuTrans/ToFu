/** Public client assembly and chat entry points. Transport and resource owners have independent lifecycles. */
import type { TofuOptions, ChatRequest, ChatCompletion } from './types.js';
import { FetchTransport, responseBody } from './transport.js';
import { TofuError, nativeRequest } from './protocol.js';
import { parseSSE } from './streaming.js';
import { TasksAPI } from './tasks.js';
import { AgentsAPI } from './agents.js';
import { KeysAPI, WebhooksAPI } from './accounts.js';

export class Tofu extends FetchTransport {

  readonly tasks: TasksAPI;
  readonly agents: AgentsAPI;
  readonly keys: KeysAPI;
  readonly webhooks: WebhooksAPI;

  constructor(opts: TofuOptions) {
    super(opts);
    this.tasks = new TasksAPI(this);
    this.agents = new AgentsAPI(this);
    this.keys = new KeysAPI(this);
    this.webhooks = new WebhooksAPI(this);
  }


  // ── Capabilities ──────────────────────────────────────────────

  capabilities(): Promise<Record<string, unknown>> {
    return this._json('GET', '/api/v1/capabilities');
  }

  // ── Chat ──────────────────────────────────────────────────────

  chat(req: ChatRequest): Promise<ChatCompletion> {
    return this._json<ChatCompletion>(
      'POST', '/api/v1/chat/completions', {
        json: nativeRequest(req, { modelRequired: true }),
      });
  }

  /** Stream chat completion as parsed SSE events. */
  async *stream(req: ChatRequest): AsyncIterable<Record<string, unknown>> {
    const resp = await this._request('POST', '/api/v1/chat/completions', {
      json: {
        ...nativeRequest(req, { modelRequired: true }),
        stream: true,
      },
      headers: { Accept: 'text/event-stream' },
    });
    if (!resp.ok) {
      const err = await responseBody(resp);
      throw new TofuError(resp.status, err);
    }
    yield* parseSSE(resp);
  }
}
