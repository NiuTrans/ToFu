/** audio endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createAudioEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const audio = {
    transcribe:   (formData: FormData) => request('/api/v4/audio/transcribe', { method: 'POST', body: formData, timeout: 0 }),
    capabilities: ()         => get('/api/v4/audio/capabilities', { onError: 'null' }),
  };
  return audio;
}
