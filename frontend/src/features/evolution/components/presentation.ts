/** Read-only goal, result and capability projections from authoritative receipts. */
import type { EvolutionState } from '../controller';
import type { EvolutionMutation } from '../../../api/evolution';
import { badge, button, copy, empty, esc, label, link, number, section } from './ui';
import { liveConversationHtml, type EpisodeActivity } from './activity';
export const mutationTitle = (m: EvolutionMutation): string => String(m.payload?.title || m.payload?.name || copy(m.kind));
const row = (id: string, title: string, detail: string, actions: string): string => `<article class="evo-row" data-key="${esc(id)}"><div class="evo-row-main"><h3>${esc(title)}</h3><div class="evo-row-meta">${detail}</div></div><div class="evo-row-actions">${actions}</div></article>`;
export function overview(state: EvolutionState): string {
  const decisions = state.plans.filter(p=>p.status==='proposed').map(p => {
    const candidate = p.candidates.find(c=>c.path_id===p.selected_path_id);
    return row(p.plan_id, state.goals.find(g=>g.goal_id===p.goal_id)?.statement || copy('missingGoal'),
      `${esc(copy('planPending'))} · ${number(candidate?.cost_estimate.expected_tokens)} tokens`,link(`goals/${encodeURIComponent(p.goal_id)}`,copy('viewPlan')));
  }).concat(state.mutations.filter(m=>['proposed','confirmed'].includes(m.status)).map(m=>row(m.mutation_id,mutationTitle(m),badge(m.activation_intent?'activationPending':m.status),link(`capabilities/${encodeURIComponent(m.mutation_id)}`,copy('viewEvidence')))));
  const running = state.episodes.filter(e=>e.status==='running').map(e=>row(e.episode_id,state.goals.find(g=>g.goal_id===e.goal_id)?.statement || copy('missingGoal'),
    `${badge(e.status)} <span>${esc(copy('taskAuthority'))}</span>`,link(`live/${encodeURIComponent(e.episode_id)}`,copy('watchLive'))));
  const changes = state.mutations.filter(m=>['active','rolled_back'].includes(m.status)).slice(-8).reverse().map(m=>row(m.mutation_id,mutationTitle(m),`${badge(m.activation_intent?'activationPending':m.status)} · v${m.version}`,link(`capabilities/${encodeURIComponent(m.mutation_id)}`,copy('viewEvidence'))));
  if (!state.goals.length && !state.mutations.length) return `<div class="evo-welcome"><h2>${esc(copy('firstGoal'))}</h2><p>${esc(copy('firstHint'))}</p>${button('new',copy('newGoal'),' ',true)}<div class="evo-example">${esc(copy('example'))}</div></div>`;
  return section(copy('decisions'),decisions.slice(0,12).join('') || empty(copy('noDecisions'))) +
    section(copy('running'),running.slice(0,12).join('') || empty(copy('noRunning'))) +
    section(copy('changes'),`<ol class="evo-evidence-chain"><li>${esc(copy('sourceRecords'))}</li><li>${esc(copy('verificationResults'))}</li><li>${esc(copy('capabilityChanges'))}</li></ol>${changes.join('') || empty(copy('noChanges'))}`);
}
export function goals(state: EvolutionState): string {
  return section(copy('goals'),state.goals.slice().reverse().map(g=>row(g.goal_id,g.statement,
    `${badge(g.status)} · ${esc(g.domain)}${g.exploratory ? ` · ${esc(copy('explore'))}` : ''}`,
    link(`goals/${encodeURIComponent(g.goal_id)}`,copy('openGoal')))).join('') || empty(copy('firstHint')));
}
export function goalDetail(state: EvolutionState, id: string): string {
  const goal = state.goals.find(g=>g.goal_id===id);
  if (!goal) return empty(copy('notFound'));
  const plans = state.plans.filter(p=>p.goal_id===id);
  const episodes = state.episodes.filter(e=>e.goal_id===id);
  const budget = goal.budget_envelope;
  const heading = `<header class="evo-detail-head">${link('goals',copy('allGoals'))}<h2>${esc(goal.statement)}</h2>${badge(goal.status)} <span class="evo-muted">${esc(goal.domain)}</span></header>`;
  const criteria = (goal.criteria || []).map(c=>`<li><strong>${esc(c.kind==='test_command'?copy('testCommand'):copy('humanCheck'))}</strong><pre>${esc(c.params.command || c.params.description || JSON.stringify(c.params))}</pre></li>`).join('');
  const method = section(copy('completion'),criteria ? `<ul class="evo-criteria">${criteria}</ul>` : empty(copy('exploreHint')));
  const budgets = section(copy('budget'),budget ? `<dl class="evo-facts"><div><dt>${esc(copy('tokens'))}</dt><dd>${number(budget.max_tokens)}</dd></div><div><dt>${esc(copy('minutes'))}</dt><dd>${number(budget.max_wall_clock_seconds/60)}</dd></div><div><dt>${esc(copy('calls'))}</dt><dd>${number(budget.max_tool_calls)}</dd></div></dl>` : empty(copy('unknown')));
  const planRows = plans.map(p=>`<article class="evo-plan" data-key="${esc(p.plan_id)}"><div class="evo-row-meta">${badge(p.status)}</div>${p.candidates.map(c=>`<div class="evo-candidate ${c.path_id===p.selected_path_id?'evo-selected':''}"><h3>${esc(c.title)} ${c.path_id===p.selected_path_id?`<small>${esc(copy('recommended'))}</small>`:''}</h3><p class="evo-muted">${number(c.cost_estimate.expected_tokens)} tokens · ${esc(copy(c.cost_estimate.priors_source))}</p>${c.stages?.length?`<ol>${c.stages.map(s=>`<li>${esc(s.detail || s.kind)}</li>`).join('')}</ol>`:''}</div>`).join('')}<p>${esc(p.selection_rationale || '')}</p><div class="evo-actions">${p.status==='proposed'?button('launch',copy('confirmLaunch'),p.plan_id,true)+button('rejectPlan',copy('reject'),p.plan_id):p.status==='confirmed'&&!episodes.some(e=>e.plan_id===p.plan_id)?button('launch',copy('launch'),p.plan_id,true):''}</div></article>`).join('');
  const planning = section(copy('plan'),planRows || button('propose',copy('createPlan'),id,true));
  const results = episodes.map(e=>`<article class="evo-episode" data-key="${esc(e.episode_id)}"><div class="evo-row-meta">${badge(e.status)}${e.exploratory?esc(copy('explore')):''}</div><p class="evo-muted">${esc(copy('recordedCost'))}: ${e.actuals_complete===false?"≥ ":""}${number(e.actuals.tokens)} tokens · ${e.actuals_complete===false?"≥ ":""}${number(e.actuals.wall_clock_seconds)} s · ${e.actuals_complete===false?"≥ ":""}${number(e.actuals.tool_calls)} ${esc(copy('calls'))}</p>
    ${e.status==='running'?`<p>${esc(copy('taskAuthority'))}</p>${link(`live/${encodeURIComponent(e.episode_id)}`,copy('watchLive'))} ${button('task',copy('taskProgress'),e.task_id)} ${button('assess',copy('checkAssessment'),e.episode_id)}<div data-key="assess-${esc(e.episode_id)}" data-evo-island class="evo-assessment"></div>`:''}
    ${e.stage_receipts?.length?`<ol>${e.stage_receipts.map(s=>`<li>${esc(s.stage)} · ${esc(s.status)} ${esc(s.detail || '')}</li>`).join('')}</ol>`:''}
    ${e.verdict?`<h3>${esc(copy('verificationResults'))}</h3><p>${esc(copy('assessedBy'))}: ${esc(copy(e.verdict.assessed_by))}</p><ul class="evo-criteria">${e.verdict.criterion_results.map(r=>`<li>${esc(r.criterion_id)} · ${esc(copy(r.outcome))}<pre>${esc(r.evidence_ref || copy('noEvidence'))}</pre></li>`).join('')}</ul>`:empty(copy('noVerdict'))}
    <h3>${esc(copy('learned'))}</h3>${e.learning && Object.values(e.learning).some(v=>Array.isArray(v)?v.length:!!v)?`<dl class="evo-facts"><div><dt>${esc(copy('resourceUpdates'))}</dt><dd>${number(e.learning.resource_weight_deltas?.length || 0)}</dd></div><div><dt>${esc(copy('costRatio'))}</dt><dd>${number(e.learning.cost_actual_vs_estimate?.tokens_ratio)}</dd></div></dl><pre>${esc(e.learning.strategy_outcome?JSON.stringify(e.learning.strategy_outcome,null,2):copy('noStrategy'))}</pre>`:empty(copy('noLearning'))}
    ${state.mutations.filter(m=>m.evidence_episode_ids?.includes(e.episode_id)).map(m=>`<p>${link(`capabilities/${encodeURIComponent(m.mutation_id)}`,mutationTitle(m))} ${badge(m.activation_intent?'activationPending':m.status)}</p>`).join('')}</article>`).join('');
  return heading+method+budgets+planning+section(copy('execution'),results || empty(copy('notStarted')))+section(copy('report'),button('report',copy('readReport'),id)+`<div data-key="report-${esc(id)}" data-evo-island class="evo-report" hidden></div>`);
}
export function capabilities(state: EvolutionState): string {
  return section(copy('skills'),state.skills.map(s=>row(s.skill_id,s.name,`${badge(s.status)} · ${esc(s.description)}`,s.sandbox_receipt?`<details><summary>${esc(copy('verificationResults'))}</summary><pre>${esc(s.sandbox_receipt.verdict)}\n${esc(s.sandbox_receipt.output)}</pre></details>`:'')).join('') || empty(copy('noSkills')))+
    section(copy('proposals'),state.mutations.map(m=>row(m.mutation_id,mutationTitle(m),`${badge(m.activation_intent?'activationPending':m.status)} · v${m.version}`,link(`capabilities/${encodeURIComponent(m.mutation_id)}`,copy('viewEvidence')))).join('') || empty(copy('noChanges')))+
    section(copy('gaps'),state.gaps.filter(g=>g.status==='proposed').map(g=>row(g.gap_id,g.summary,`${number(g.samples)} · ${esc(copy('observations'))}`,link(`teach/${encodeURIComponent(g.gap_id)}`,copy('teach')))).join('') || empty(copy('noGaps')))+
    section(copy('systemKnowledge'),state.snapshot?`<dl class="evo-facts"><div><dt>${esc(copy('strategies'))}</dt><dd>${number(state.snapshot.strategies)}</dd></div><div><dt>${esc(copy('antiPatterns'))}</dt><dd>${number(state.snapshot.anti_patterns)}</dd></div></dl><p class="evo-muted">${esc(copy('snapshotHint'))}</p>`:empty(copy('unknown')));
}
/** Live conversation workbench for one running episode (single agent). */
export function liveEpisode(state: EvolutionState, episodeId: string, activity: EpisodeActivity | undefined): string {
  const episode = state.episodes.find(e=>e.episode_id===episodeId);
  if (!episode) return empty(copy('notFound'));
  const goal = state.goals.find(g=>g.goal_id===episode.goal_id);
  const title = goal?.statement || copy('missingGoal');
  const head = `<header class="evo-detail-head">${link(`goals/${encodeURIComponent(episode.goal_id)}`,copy('openGoal'))}<h2>${esc(title)}</h2>${badge(episode.status)}</header>`;
  if (episode.status !== 'running') {
    return head + empty(copy('episodeSettled')) + `<div class="evo-actions">${link(`goals/${encodeURIComponent(episode.goal_id)}`,copy('viewResults'))}</div>`;
  }
  const act = activity || { stage: copy('stageStarting'), stageDone: false, rounds: [], hasRounds: false };
  return head + liveConversationHtml(title, act);
}

/** Management-floor view: one workstation per running agent with a live bubble. */
export function floor(state: EvolutionState, bubbles: Record<string, string>): string {
  const running = state.episodes.filter(e=>e.status==='running');
  if (!running.length) {
    return `<div class="evo-floor-empty"><h2>${esc(copy('floorTitle'))}</h2><p>${esc(copy('floorEmpty'))}</p><div class="evo-actions">${link('',copy('overview'))} ${link('new',copy('newGoal'))}</div></div>`;
  }
  const stations = running.map(e=>{
    const goal = state.goals.find(g=>g.goal_id===e.goal_id);
    const bubble = bubbles[e.episode_id] || copy('stageStarting');
    return `<button type="button" class="evo-station" data-evo-action="openLive" data-id="${esc(e.episode_id)}">` +
      `<span class="evo-station-bubble">${esc(bubble)}</span>` +
      `<span class="evo-station-desk"><span class="evo-station-avatar" aria-hidden="true"></span></span>` +
      `<span class="evo-station-name">${esc(goal?.statement || copy('missingGoal'))}</span>` +
      `<span class="evo-station-meta">${esc(copy('floorOpen'))}</span>` +
      `</button>`;
  }).join('');
  return `<div class="evo-floor"><h2>${esc(copy('floorTitle'))}</h2><p class="evo-muted">${esc(copy('floorHint'))}</p><div class="evo-floor-grid">${stations}</div></div>`;
}
export function mutationDetail(state: EvolutionState, id: string): string {
  const m = state.mutations.find(m=>m.mutation_id===id); if(!m) return empty(copy('notFound'));
  const pending = Boolean(m.activation_intent);
  const evidence = (m.evidence_episode_ids || []).map(id=>{const e=state.episodes.find(e=>e.episode_id===id);return `<li>${e?link(`goals/${encodeURIComponent(e.goal_id)}`,state.goals.find(g=>g.goal_id===e.goal_id)?.statement||id):esc(id)} ${e?badge(e.status):esc(copy('outsideWindow'))}</li>`;}).join('');
  return `<header class="evo-detail-head">${link('capabilities',copy('capabilities'))}<h2>${esc(mutationTitle(m))}</h2>${badge(m.activation_intent?'activationPending':m.status)} <span class="evo-muted">v${m.version} · ${esc(copy('parentVersion'))} ${m.parent_version ?? '—'}</span></header>`+
    section(copy('sourceRecords'),`<ul class="evo-criteria">${evidence}</ul>`)+section(copy('changeDetails'),`<pre>${esc(JSON.stringify(m.payload || {},null,2))}</pre>`)+
    section(copy('verificationResults'),`<p>${esc(copy(pending?'activationHint':'gateHint'))}</p><div data-key="gate-${esc(id)}${pending?'-pending':''}" data-evo-island class="evo-gate-result" role="status"></div><div class="evo-actions">${pending?button('enable',copy('continueActivation'),id,true):['proposed','confirmed'].includes(m.status)?button('enable',copy('verifyEnable'),id,true)+button('rejectMutation',copy('reject'),id):m.status==='active'?button('rollback',copy('rollback'),id):''}</div>${m.rejection_reason?`<p>${esc(m.rejection_reason)}</p>`:''}`);
}
