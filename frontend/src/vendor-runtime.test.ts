// @vitest-environment jsdom
/** The application and sandboxed artifacts share the locked math renderer. */
import artifactSource from '../../static/vendor/katex/katex.min.js?raw';
import lock from '../../package-lock.json';
import { describe, expect, it } from 'vitest';
import { loadKatex, type KatexModule } from './vendor-runtime';

function artifactKatex(): KatexModule {
  const sandbox = {} as { katex: KatexModule };
  // Exercise the browser UMD branch with an isolated global binding.
  new Function('self', 'exports', 'module', 'define', artifactSource)(sandbox);
  return sandbox.katex;
}

describe('math dependency delivery', () => {
  it('uses the same locked version in the application and artifact renderer', async () => {
    const app = await loadKatex();
    expect(app.version).toBe(lock.packages['node_modules/katex'].version);
    expect(artifactKatex().version).toBe(app.version);
  });

  it.each(['application', 'artifact'] as const)('%s ignores inherited trust permission', async (surface) => {
    const renderer = surface === 'application' ? await loadKatex() : artifactKatex();
    const options = Object.create({ trust: true });
    options.throwOnError = false;
    const host = document.createElement('div');
    host.innerHTML = renderer.renderToString('\\href{javascript:alert(1)}{untrusted}', options);
    expect(host.querySelector('[href^="javascript:"]')).toBeNull();
    expect(host.textContent).toContain('untrusted');
  });
});
