import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('myday', name, args, stub);
}
