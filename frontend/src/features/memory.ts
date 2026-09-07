import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import './memory/panel';
import './memory/preferences';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('memory', name, args, stub);
}
