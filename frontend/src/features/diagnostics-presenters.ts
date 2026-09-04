/** Demand-loaded Debug and Request Inspector presentation entry. */
import '../runtime/diagnostics-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('diagnostics-presenters', name, args, stub);
}
