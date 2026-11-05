/** Import one same-mount plugin module and hand it explicit host ports.
 * No plugin names, copied dependency graphs or application globals live here.
 */
import type { PluginHostV1, PluginPageModule } from './ports';

export async function mountPluginPage(
  document: Document,
  host: PluginHostV1,
  loadModule: (url: string) => Promise<PluginPageModule>,
): Promise<void> {
  const body = document.body;
  body.dataset.tofuPluginState = 'loading';
  try {
    const source = body.dataset.tofuPluginModule;
    const base = new URL('.', document.URL);
    if (!source) throw new Error('Plugin module is not declared');
    const url = new URL(source, base);
    if (!url.href.startsWith(base.href)) throw new Error('Plugin module leaves the page mount');
    const plugin = await loadModule(url.href);
    if (typeof plugin.mount !== 'function') throw new Error('Plugin module has no mount export');
    await plugin.mount(host);
    body.dataset.tofuPluginState = 'ready';
  } catch (error) {
    body.dataset.tofuPluginState = 'error';
    const alert = document.querySelector<HTMLElement>('[data-tofu-plugin-error]');
    if (alert) alert.hidden = false;
    throw error;
  }
}
