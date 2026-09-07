/** Lazy Project Brain entry point.
 *
 * The generated side-effect module composes the retained authored sections
 * declared by runtime/sections/manifest.json. It registers real owners in the
 * private feature registry before this entry dispatches the triggering action.
 */
import '../runtime/project-brain-runtime.generated.js';
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('project-brain', name, args, stub);
}
