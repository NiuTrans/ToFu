/** Demand-loaded owner for update, timer, and optimizer utility panels. */
import '../runtime/utility-panels-runtime.generated.js';
import {
  announceFeatureDomainLoaded,
  invokeFeatureEntry,
  type FeatureCallable,
} from '../runtime-bridge';

export async function prepare(): Promise<void> {
  announceFeatureDomainLoaded('utility-panels');
}

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('utility-panels', name, args, stub);
}
