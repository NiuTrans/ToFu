import { resolveRuntimeAction, type RuntimeCallable } from './runtime/app-runtime.js';
import { getFeatureBinding } from './feature-registry';

export type FeatureCallable = RuntimeCallable;

export function announceFeatureDomainLoaded(domain: string): void {
  document.dispatchEvent(new CustomEvent('tofu:feature-domain-loaded', {
    detail: { domain, source: 'vite' },
  }));
}

export function invokeFeatureEntry(
  domain: string,
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): unknown {
  announceFeatureDomainLoaded(domain);
  // The retained ESM runtime installs a lazy routing stub in runtimeScope so
  // shell actions can request their owning chunk.  featureRegistry falls back
  // to that service table, therefore its first answer can legitimately be the
  // same stub that entered this function.  Once the domain chunk has evaluated,
  // prefer a module override; otherwise resolve the real lexical runtime owner
  // instead of treating the routing stub as a missing implementation.
  const registered = getFeatureBinding(name);
  const candidate = registered === stub
    ? resolveRuntimeAction(name)
    : (registered ?? resolveRuntimeAction(name));
  if (typeof candidate !== 'function' || candidate === stub) {
    throw new Error(`${name} was not defined by the required ${domain} module`);
  }
  return (candidate as FeatureCallable)(...args);
}
