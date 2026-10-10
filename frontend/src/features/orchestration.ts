import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './orchestration-core-owners';
import './orchestration-view-owners';
import './orchestration-studio-view-owners';
import '../runtime/orchestration-presenters.generated.js';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('orchestration', name, args, stub);
}
