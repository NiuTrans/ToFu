/** Public v1 browser host ports; plugin pages own their views and lifecycles.
 * API behavior comes from the same endpoint registry as the main application.
 */
import { createEndpointRegistry } from '../api/endpoint-registry';
import { apiTransport } from '../api/transport';
import { DOMPurify, marked } from '../markdown-vendors';

export interface PluginHostV1 {
  readonly version: 1;
  readonly api: ReturnType<typeof createEndpointRegistry>;
  readonly markdown: {
    parse(source: string): string;
    sanitize(html: string): string;
  };
}

export interface PluginPageModule {
  mount(host: PluginHostV1): void | Promise<void>;
}

function parseMarkdown(source: string): string {
  const html = marked.parse(source, { async: false });
  if (typeof html !== 'string') throw new TypeError('The plugin Markdown parser must be synchronous');
  return html;
}

export function createPluginHost(document: Document): PluginHostV1 {
  return Object.freeze({
    version: 1,
    api: createEndpointRegistry(apiTransport, () => document.documentElement.lang || 'zh'),
    markdown: Object.freeze({
      parse: parseMarkdown,
      sanitize: (html: string) => DOMPurify.sanitize(html),
    }),
  });
}
