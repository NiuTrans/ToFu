import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('infrastructure', name, args, stub);
}
