/** Demand-loaded owner for creative image generation and result retries. */
import '../runtime/image-generation-runtime.generated.js';
import {
  announceFeatureDomainLoaded,
  invokeFeatureEntry,
  type FeatureCallable,
} from '../runtime-bridge';

export async function prepare(): Promise<void> {
  announceFeatureDomainLoaded('image');
}

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('image', name, args, stub);
}
