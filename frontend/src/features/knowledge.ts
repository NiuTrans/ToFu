/** Demand-loaded local Knowledge Workbench presentation entry. */
import '../runtime/knowledge-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('knowledge', name, args, stub);
}
