import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('project-brain', name, args, stub);
}
