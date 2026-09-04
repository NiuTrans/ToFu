import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry } from '../feature-registry';
import './orchestration/task-mode.css';
import './orchestration-core-owners';
import './orchestration-view-owners';
import './orchestration-studio-view-owners';
import { orchestrationRegistry } from './orchestration/registry';
import '../runtime/orchestration-presenters.generated.js';

// Task Mode is a typed owner rather than a retained function declaration.
// Publish its routed entries through the same private feature boundary after
// every registry owner and the retained Studio runtime have settled.
for (const name of ['openTaskMode', 'closeTaskMode'] as const) {
  const owner = orchestrationRegistry[name];
  if (typeof owner === 'function') featureRegistry[name] = owner;
}

export async function invoke(
  name: string,
  args: readonly unknown[],
  stub: FeatureCallable,
): Promise<unknown> {
  return invokeFeatureEntry('orchestration', name, args, stub);
}
