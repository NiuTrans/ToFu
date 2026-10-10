/** videos endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createVideosEndpoints(ports: EndpointPorts) {
  const { request, get } = ports;
  const videos = {
    upload: (formData: FormData) => request('/api/v4/videos/upload', { method: 'POST', body: formData, timeout: 0 }),
    status: (videoId: string)  => get(`/api/v4/videos/${encodeURIComponent(videoId)}`, { onError: 'null' }),
  };
  return videos;
}
