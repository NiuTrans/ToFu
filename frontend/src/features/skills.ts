import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './skills/panel';

/** Lazy Skills domain entry; behavior is installed by the panel owner. */
export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('skills', name, args, stub);
}
