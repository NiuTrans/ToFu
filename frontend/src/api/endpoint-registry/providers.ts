/** providers endpoint facade. Owns wire paths and projections; transport/retry policy arrives through explicit ports. */
import type { EndpointPorts } from './contracts';
export function createProvidersEndpoints(ports: EndpointPorts) {
  const { get, post, requiredItemsList } = ports;
  const providers = {
    templates: async () => {
      const path = '/api/v4/providers/templates';
      return requiredItemsList(await get(path), path);
    },
    compileTemplate: (templateKey: string, selectedModelIds: unknown[]) =>
      post('/api/v4/providers/templates/compile', {
        template_key: templateKey,
        selected_model_ids: selectedModelIds,
      }),
    probe:            (baseUrl: string, apiKey: string, modelsPath: string)     =>
      post('/api/v4/providers/probe',
           { base_url: baseUrl, api_key: apiKey, models_path: modelsPath },
           { onError: 'null' }),
  };
  return providers;
}
