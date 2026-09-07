/** Demand-loaded Local Control modal, capability probes, and agent relay. */
import '../runtime/local-control-presenters.generated.js';
import { getFeatureBinding } from '../feature-registry';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

const AGENT_RELAY_DEEP_LINK_DURATION_MS = 30 * 60 * 1000;

export async function prepare(name: string): Promise<void> {
  if (name !== '_lcEnsureAgentRelay') return;
  const startRelay = getFeatureBinding(name);
  if (typeof startRelay !== 'function') {
    throw new Error('Local Control relay owner did not register');
  }
  await Promise.resolve(startRelay(AGENT_RELAY_DEEP_LINK_DURATION_MS));
}

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('local-control', name, args, stub);
}
