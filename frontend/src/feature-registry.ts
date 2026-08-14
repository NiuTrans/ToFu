/**
 * Module-private port used while the retained runtime is split into typed
 * feature owners. Reads fall through to the ESM runtime service table; writes
 * remain private and update a writable runtime accessor when one exists.
 */
const overrides: Record<PropertyKey, unknown> = Object.create(null);
type RuntimeReader = (name: string) => unknown;
type RuntimeWriter = (name: string, value: unknown) => void;
let readRuntime: RuntimeReader = (name) => {
  if (typeof window === 'undefined') return undefined;
  return (window as unknown as Record<string, unknown>)[name];
};
let writeRuntime: RuntimeWriter = () => undefined;

/** Inject the retained runtime port from the main entry without making every
 * independently testable feature owner import the whole application shell. */
export function connectFeatureRuntime(
  reader: RuntimeReader,
  writer: RuntimeWriter,
): void {
  readRuntime = reader;
  writeRuntime = writer;
  // A test harness or an alternate entry may connect after feature modules
  // have evaluated. Replay their already-registered owners into the injected
  // service table so connection order never changes the resulting graph.
  for (const property of Reflect.ownKeys(overrides)) {
    if (typeof property !== 'string') continue;
    try { writeRuntime(property, overrides[property]); } catch { /* read-only */ }
  }
}

export const featureRegistry = new Proxy(overrides, {
  get(target, property): unknown {
    if (Object.prototype.hasOwnProperty.call(target, property)) {
      return target[property];
    }
    if (typeof property !== 'string') return undefined;
    const runtime = readRuntime(property);
    if (runtime !== undefined) return runtime;
    if (property === 'Api') return window.Api;
    if (property === 'TofuModules') return window.TofuModules;
    return undefined;
  },
  set(target, property, value): boolean {
    target[property] = value;
    if (typeof property === 'string') {
      try { writeRuntime(property, value); } catch { /* read-only service */ }
    }
    return true;
  },
});

export function getFeatureBinding(name: string): unknown {
  return featureRegistry[name];
}
