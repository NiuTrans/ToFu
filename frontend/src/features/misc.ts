import { invokeFeatureEntry, type FeatureCallable } from '../runtime-bridge';

const entryFiles: Readonly<Record<string, readonly string[]>> = Object.freeze({
  openKnowledgeBase: ['knowledge.js'],
  closeKnowledgeBase: ['knowledge.js'],
  openProjectModal: ['project.js'],
  closeProjectModal: ['project.js'],
  resolveWriteApproval: ['project.js'],
  submitStdinInput: ['project.js'],
  submitStdinEof: ['project.js'],
  submitHumanGuidanceChoice: ['project.js'],
  submitHumanGuidanceFreeText: ['project.js'],
  undoConvModifications: ['project.js'],
  undoAllModifications: ['project.js'],
  redoConvModifications: ['project.js'],
  openApplyModal: ['project.js'],
  closeApplyModal: ['project.js'],
  confirmApplyCode: ['project.js'],
  _toggleCostPopover: ['ui/finish_info_rich.js'],
  openUpdateDialog: ['update.js'],
  closeUpdateModal: ['update.js'],
  _renderSettingsUpdatePill: ['update.js'],
  toggleTimerPanel: ['timer.js'],
  toggleOptimizerPanel: ['optimizer.js'],
  _populateToolsTab: ['tools_panel.js'],
  _toolsInvSearch: ['tools_panel.js'],
});

export function supports(name: string): boolean {
  return Object.hasOwn(entryFiles, name);
}

export async function prepare(name: string): Promise<void> {
  const files = entryFiles[name];
  if (!files) throw new Error(`No frontend owner for deferred entry: ${name}`);
}

export async function invoke(name: string, args: readonly unknown[], stub: FeatureCallable): Promise<unknown> {
  await prepare(name);
  return invokeFeatureEntry('misc', name, args, stub);
}
