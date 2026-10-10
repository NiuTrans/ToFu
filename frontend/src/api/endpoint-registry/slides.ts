/** slides endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createSlidesEndpoints(ports: EndpointPorts) {
  const { request, post, _resolve } = ports;
  const slides = {
    pageFileUrl: (taskId: string, n: number, rev: number)     =>
      _resolve(`/api/v4/slides/${encodeURIComponent(taskId)}/pages/` +
               `${encodeURIComponent(n)}.png` + (rev ? `?v=${encodeURIComponent(rev)}` : '')),
    pageHtmlUrl: (taskId: string, n: number)          =>
      _resolve(`/api/v4/slides/${encodeURIComponent(taskId)}/pages/` +
               `${encodeURIComponent(n)}.html`),
    pageElements: (taskId: string, n: number)         =>
      request(`/api/v4/slides/${encodeURIComponent(taskId)}/pages/` +
              `${encodeURIComponent(n)}/elements`, { method: 'GET', onError: 'null' }),
    patchPage: (taskId: string, n: number, ops: unknown[])       =>
      post(`/api/v4/slides/${encodeURIComponent(taskId)}/pages/` +
           `${encodeURIComponent(n)}/patch`, { ops }, { onError: 'null' }),
  };
  return slides;
}
