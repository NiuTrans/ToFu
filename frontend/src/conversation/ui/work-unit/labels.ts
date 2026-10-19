/** Human activity labels projected from server stages. Unknown stages remain visible as evidence. */
import type { I18nKey } from '../../../i18n';
import { phrase, ready, terminal, type WorkText, type WorkUnit } from './contracts';
const statuses: Record<string, [I18nKey, string]> = {
  queued: ['workUnit.queued', 'Queued'], running: ['workUnit.running', 'In progress'],
  retrying: ['workUnit.retrying', 'Retrying'], done: ['wu.doneLine', 'Finished'],
  degraded: ['wu.degradedLine', 'Finished with reduced quality'], failed: ['wu.failedLine', 'Job failed'],
  aborted: ['wu.abortedLine', 'Stopped'],
};
const stages: Record<string, [I18nKey, string]> = {
  research: ['workUnit.stageResearch', 'Gathering sources'], outline: ['workUnit.stageOutline', 'Planning the story'],
  script: ['workUnit.stageOutline', 'Planning the story'], design: ['workUnit.stageDesign', 'Designing the presentation'],
  author: ['workUnit.stageAuthor', 'Creating the content'], assets: ['workUnit.stageAssets', 'Preparing media'],
  render: ['workUnit.stageRender', 'Rendering previews'], layout_qa: ['workUnit.stageLayout', 'Checking layout'],
  visual_qa: ['workUnit.stageVisual', 'Reviewing visual quality'], export: ['workUnit.stageExport', 'Exporting the deliverable'],
  publish: ['workUnit.stagePublish', 'Publishing the result'], timeline: ['workUnit.stageTimeline', 'Arranging scenes and narration'],
  outline_gate: ['workUnit.stageOutline', 'Planning the story'], storyboard_gate: ['workUnit.stageOutline', 'Planning the story'],
};
export function statusLabel(unit: WorkUnit, t: WorkText): string {
  if (!terminal(unit) && unit.pendingGate?.guidanceId) return t('workUnit.needsInput', 'Needs your input');
  const pair = statuses[unit.status] ?? ['workUnit.unavailable', 'Progress unavailable'];
  return t(pair[0], pair[1]);
}
export function stageLabel(name: string, t: WorkText): string { const pair = stages[name]; return pair ? t(...pair) : name; }
export function activityLabel(unit: WorkUnit, t: WorkText): string {
  if (!terminal(unit) && unit.pendingGate?.guidanceId) return t('workUnit.reviewPrompt', 'Review the plan to continue.');
  if (ready(unit)) return unit.artifacts?.length || unit.pages?.rendered.length
    ? t('workUnit.ready', 'Ready to preview, download or revise.') : t('workUnit.completed', 'Production completed.');
  if (unit.status === 'failed') return unit.failure?.message || t('wu.failedLine', 'Job failed');
  if (unit.status === 'aborted') return t('workUnit.stopped', 'Production stopped. Existing previews are retained.');
  const active = Array.isArray(unit.stages) ? unit.stages.filter(stage => stage.state === 'active') : [];
  return active.length ? active.map(stage => stageLabel(stage.name, t)).join(' · ') : statusLabel(unit, t);
}
export function pageProgress(unit: WorkUnit, t: WorkText): string {
  if (!unit.pages || !Array.isArray(unit.pages.rendered)) return '';
  return phrase(t, 'workUnit.pageProgress', '{n}/{total} page previews', { n: unit.pages.rendered.length, total: unit.pages.total });
}
