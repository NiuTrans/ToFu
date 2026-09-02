import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './orchestration/task-mode.css';

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('orchestration', name, args, stub);
}
