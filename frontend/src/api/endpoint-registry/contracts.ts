/** Compatibility endpoint inputs. HTTP/SSE mechanics and error identity remain owned by api/transport.ts. */
import type { RequestOptions } from '../transport';
import type { createEndpointPorts } from './ports';
export type JsonObject = Record<string, unknown>;
export interface EndpointOptions extends RequestOptions {
  before?: number; beforeId?: string; limit?: number; window?: number; prefetch?: boolean;
}
export type EndpointPorts = ReturnType<typeof createEndpointPorts>;
export type JsonRequest = <T = JsonObject | null>(path: string, options?: RequestOptions | null) => Promise<T>;
