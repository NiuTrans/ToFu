/** media endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createMediaEndpoints(ports: EndpointPorts) {
  const { request, get, del } = ports;
  const media = {
    upload: (formData: FormData, options: { signal?: AbortSignal } = {}) => request('/api/v4/media/attachments', {
      method: 'POST', body: formData, timeout: 0, signal: options.signal,
    }),
    // .zip/.tar(.gz) drop on the composer → stored verbatim (never extracted)
    // in a fresh managed workspace directory. Needs no pre-attached project.
    storeArchive: (formData: FormData, options: { signal?: AbortSignal } = {}) => request('/api/v4/media/attachments/archive', {
      method: 'POST', body: formData, timeout: 0, signal: options.signal,
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
