/** audio endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
import type { RequestOptions } from '../transport';
export function createAudioEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const audio = {
    transcribe: (formData: FormData, options: Pick<RequestOptions, 'signal'> = {}) =>
      request('/api/v4/audio/transcribe', { method: 'POST', body: formData, timeout: 0, ...options }),
    capabilities: (options: Pick<RequestOptions, 'signal'> = {}) =>
      get('/api/v4/audio/capabilities', { onError: 'null', ...options }),
  };
  return audio;
}
