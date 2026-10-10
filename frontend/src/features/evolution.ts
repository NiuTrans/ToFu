import '../styles/application/26-evolution.css';
/** Lazy Evolution composition: only this adapter knows the application shell. */
import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';
import { featureRegistry, readLiveRuntimeBinding } from '../feature-registry';
import { recoverFeatureLoad } from '../core/feature-load-recovery';
import { evolutionApi } from '../api/evolution';
import { mountEvolutionWorkspace } from './evolution/workspace';
import { goToWorkspace, hasMountedWorkspace, mountWorkspacePage, showWorkspace, workspacePath } from '../application-shell/page-host';
export function openEvolution(): void {
  if(hasMountedWorkspace('evolution')) {
    showWorkspace('evolution'); return;
  }
  const legacy = featureRegistry as unknown as Record<string, any>;
  if(legacy.paperMode)legacy.exitPaperMode?.();
  if(legacy.imageGenMode)legacy.exitImageGenMode?.();
  showWorkspace('evolution');
  const root=document.getElementById('evolutionPageContainer');
  if(!root)throw new Error('Evolution page container is missing');
  const page=mountEvolutionWorkspace(root,{api:evolutionApi,path:workspacePath(),navigate:goToWorkspace,
    draftOwner:()=>readLiveRuntimeBinding('_currentUserId'),draftScope:location.pathname,
    openTask:id=>legacy.openRequestInspectorForTask?.(id),
    recoverLoadFailure:error=>recoverFeatureLoad('openEvolution',error)});
  mountWorkspacePage(page.dispose,page.updateRoute);
}
(featureRegistry as unknown as Record<string, unknown>).openEvolution = openEvolution;
export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  return invokeFeatureEntry('evolution', name, args, stub);
}
