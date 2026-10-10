/** media endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createMediaEndpoints(ports: EndpointPorts) {
  const { request, get, del } = ports;
  const media = {
    upload: (formData: FormData) => request('/api/v4/media/attachments', {
      method: 'POST', body: formData, timeout: 0,
    }),
    // Bounded .zip/.tar(.gz) drop on the composer → atomic extract into a
    // managed workspace + TeX candidates. Needs no pre-attached project.
    expandArchive: (formData: FormData) => request('/api/v4/media/attachments/expand-archive', {
      method: 'POST', body: formData, timeout: 0,
    }),
    get: (attachmentId: string) => get(
      `/api/v4/media/attachments/${encodeURIComponent(attachmentId)}`,
      { onError: 'null' },
    ),
    remove: (attachmentId: string) => del(
      `/api/v4/media/attachments/${encodeURIComponent(attachmentId)}`,
      { query: { draft: 1 }, onError: 'null' },
    ),
  };
  return media;
}
