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
let runtimeConnected = false;

/** Inject the retained runtime port from the main entry without making every
 * independently testable feature owner import the whole application shell. */
export function connectFeatureRuntime(
  reader: RuntimeReader,
  writer: RuntimeWriter,
): void {
  readRuntime = reader;
  writeRuntime = writer;
  runtimeConnected = true;
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

/** Read mutable retained state without letting a registered feature owner
 * shadow its live accessor. Use this only for state whose retained section is
 * still authoritative; feature commands continue to resolve via registry. */
export function readLiveRuntimeBinding(name: string): unknown {
  return readRuntime(name);
}

/** Update mutable retained state through its injected writer. Isolated owner
 * harnesses connect later (or not at all), so their window remains the safe
 * pre-connection state port without publishing feature owners there. */
export function writeLiveRuntimeBinding(name: string, value: unknown): void {
  if (runtimeConnected) {
    writeRuntime(name, value);
    return;
  }
  if (typeof window !== 'undefined') {
    (window as unknown as Record<string, unknown>)[name] = value;
  }
}
