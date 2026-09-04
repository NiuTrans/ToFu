/** Demand-loaded compaction snapshot drawer presentation entry. */
import '../runtime/compaction-viewer-presenters.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('compaction-viewer-presenters', name, args, stub);
}
