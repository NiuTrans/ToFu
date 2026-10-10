/** images endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts, EndpointOptions, JsonObject } from './contracts';
export function createImagesEndpoints(ports: EndpointPorts) {
  const { request, get, post } = ports;
  const images = {
    generate: async (body: JsonObject, opts?: EndpointOptions) => {
      const resp = await request<Response>('/api/v4/images/generate', Object.assign({
        method: 'POST', json: body, timeout: 0, parse: 'response',
      }, opts || {}));
      const data: JsonObject = await resp.json().catch(() => ({}));
      data._status = resp.status;
      return data;
    },
    models:   ()           => get('/api/v4/images/models'),
    // upload accepts either a JSON body (legacy {base64, mediaType})
    // or a FormData. Returns {url} on success, null on failure.
    upload: (payload: JsonObject | FormData, opts?: EndpointOptions) => {
      if (typeof FormData !== 'undefined' && payload instanceof FormData) {
        return request('/api/images/upload', Object.assign({ method: 'POST', body: payload, timeout: 0 }, opts || {}));
      }
      return post('/api/images/upload', payload, Object.assign({ onError: 'null' }, opts || {}));
    },
  };
  return images;
}
