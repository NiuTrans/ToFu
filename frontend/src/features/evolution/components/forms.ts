/** User-owned form islands; polling never rebuilds inputs or operation results. */
import type { EvolutionGoal, EvolutionEpisode } from '../../../api/evolution';
import type { EvolutionApi } from '../controller';
import { copy, esc } from './ui';
const field = (name: string, title: string, control: string): string => `<label class="evo-field" for="evo-${name}"><span>${esc(title)}</span>${control}</label>`;
const input = (name: string, value = '', type = 'text', required = false): string => `<input id="evo-${name}" name="${name}" type="${type}" value="${esc(value)}" ${required ? 'required' : ''} ${type === 'number' ? 'min="1" step="1"' : 'maxlength="4000"'}>`;
export function goalForm(host: HTMLElement, api: EvolutionApi, saved: (id: string) => void): void {
  host.innerHTML = `<form class="evo-form">
    <h2>${esc(copy('newGoal'))}</h2>
    ${field('statement', copy('statement'), `<textarea id="evo-statement" name="statement" rows="3" maxlength="4000" required placeholder="${esc(copy('example'))}"></textarea>`)}
    <div class="evo-form-grid">${field('domain', copy('domain'), input('domain','general','text',true))}
    ${field('verification', copy('verification'), `<select id="evo-verification" name="verification"><option value="test_command">${esc(copy('testCommand'))}</option><option value="explicit_human">${esc(copy('humanCheck'))}</option><option value="explore">${esc(copy('explore'))}</option></select>`)}</div>
    ${field('criterion', copy('criterion'), input('criterion','pytest -q','text',true))}
    <p class="evo-muted" data-form-hint>${esc(copy('testHint'))}</p>
    <h3>${esc(copy('budget'))}</h3><p class="evo-muted">${esc(copy('budgetHint'))}</p>
    <div class="evo-form-grid evo-three">${field('tokens',copy('tokens'),input('tokens','200000','number',true))}${field('minutes',copy('minutes'),input('minutes','120','number',true))}${field('calls',copy('calls'),input('calls','200','number',true))}</div>
    <p class="evo-error" role="alert" hidden></p>
    <button type="submit" class="evo-button evo-primary">${esc(copy('createPlan'))}</button>
  </form>`;
  const form = host.querySelector('form')!;
  const mode = form.elements.namedItem('verification') as HTMLSelectElement;
  const criterion = form.elements.namedItem('criterion') as HTMLInputElement;
  mode.addEventListener('change', () => {
    criterion.disabled = mode.value === 'explore'; criterion.required = !criterion.disabled;
    criterion.value = '';
    form.querySelector('[data-form-hint]')!.textContent = copy(mode.value === 'explore' ? 'exploreHint' : mode.value === 'explicit_human' ? 'humanHint' : 'testHint');
  });
  let createdId = '';
  form.addEventListener('submit', async event => {
    event.preventDefault(); const submit = form.querySelector<HTMLButtonElement>('[type=submit]')!;
    if (submit.disabled || !form.reportValidity()) return;
    submit.disabled = true;
    const error = form.querySelector<HTMLElement>('[role=alert]')!; error.hidden = true;
    const values = new FormData(form);
    try {
      if (!createdId) {
        const exploratory = mode.value === 'explore';
        const result = await api.createGoal({schema_version: 1, goal_id: `goal-${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`,
          statement: String(values.get('statement')).trim(), domain: String(values.get('domain')).trim().slice(0,80),
          exploratory, criteria: exploratory ? [] : [{criterion_id:'c1',kind: mode.value,
            params: mode.value === 'test_command' ? {command:criterion.value.trim()} : {description:criterion.value.trim()}}],
          budget_envelope: {max_tokens:Number(values.get('tokens')),max_wall_clock_seconds:Number(values.get('minutes'))*60,max_tool_calls:Number(values.get('calls'))},
          status:'draft',created_at:Math.floor(Date.now()/1000)});
        createdId = result.goal.goal_id;
        // Once admitted, retry only planning, with the exact saved goal.
        form.querySelectorAll<HTMLInputElement>('input,textarea,select').forEach(control => { control.disabled = true; });
      }
      await api.proposePlan(createdId);
      saved(createdId);
    } catch (reason) {
      error.textContent = `${createdId ? copy('planRetry') + ' ' : ''}${reason instanceof Error ? reason.message : String(reason)}`;
      error.hidden = false;
    } finally { submit.disabled = false; }
  });
}
export function teachForm(host: HTMLElement, api: EvolutionApi, gap: {gap_id: string; summary: string}, saved: () => void): void {
  host.innerHTML = `<form class="evo-form"><h2>${esc(copy('teach'))}</h2><p>${esc(gap.summary)}</p>
    ${field('knowledge',copy('knowledge'),'<textarea id="evo-knowledge" name="knowledge" rows="5" maxlength="12000" required></textarea>')}
    ${field('examples',copy('examples'),'<textarea id="evo-examples" name="examples" rows="3" maxlength="8000" required></textarea>')}
    <p class="evo-muted">${esc(copy('teachHint'))}</p><p role="status"></p>
    <button class="evo-button evo-primary" type="submit">${esc(copy('teachVerify'))}</button></form>`;
  const form = host.querySelector('form')!;
  form.addEventListener('submit', async event => {
    event.preventDefault(); const submit = form.querySelector<HTMLButtonElement>('button')!;
    if (submit.disabled || !form.reportValidity()) return;
    submit.disabled = true; const status = form.querySelector('[role=status]')!;
    status.textContent = copy('teaching');
    try {
      const data = new FormData(form);
      const result = await api.teach({name:gap.summary.slice(0,40),description:gap.summary,
        knowledge:String(data.get('knowledge')),examples:String(data.get('examples')).split('\n').map(s=>s.trim()).filter(Boolean),gap_id:gap.gap_id});
      status.textContent = copy(result.mutation ? 'teachReady' : 'teachFailed'); saved();
    } catch (reason) { status.textContent = reason instanceof Error ? reason.message : String(reason); }
    finally { submit.disabled = false; }
  });
}

/** Only offered after an authoritative terminal task snapshot; backend rechecks settlement. */
export function assessmentForm(host: HTMLElement, api: EvolutionApi, episode: EvolutionEpisode, goal: EvolutionGoal | undefined, saved: () => void): void {
  host.innerHTML=`<form class="evo-form"><h3>${esc(copy('humanCheck'))}</h3>
    <label class="evo-field"><span>${esc(copy('assessmentOutcome'))}</span><select name="outcome"><option value="success">${esc(copy('pass'))}</option><option value="partial">${esc(copy('partial'))}</option><option value="failure">${esc(copy('fail'))}</option></select></label>
    <label class="evo-field"><span>${esc(copy('assessmentEvidence'))}</span><textarea name="evidence" maxlength="500" required></textarea></label>
    ${(goal?.criteria || []).map(c=>`<label class="evo-field"><span>${esc(c.criterion_id)} · ${esc(c.params.description || c.params.command || c.kind)}</span><select name="criterion-${esc(c.criterion_id)}"><option value="unknown">${esc(copy('unknown'))}</option><option value="pass">${esc(copy('pass'))}</option><option value="fail">${esc(copy('fail'))}</option></select></label>`).join('')}
    <p role="status"></p><button class="evo-button" type="submit">${esc(copy('saveAssessment'))}</button></form>`;
  const form=host.querySelector('form')!;
  form.addEventListener('submit',async event=>{
    event.preventDefault();const submit=form.querySelector<HTMLButtonElement>('button')!;
    if(submit.disabled||!form.reportValidity())return;submit.disabled=true;
    const data=new FormData(form), status=form.querySelector('[role=status]')!;
    try {await api.settleVerdict(episode.episode_id,String(data.get('outcome')),(goal?.criteria||[]).map(c=>({criterion_id:c.criterion_id,outcome:String(data.get(`criterion-${c.criterion_id}`)),evidence_ref:String(data.get('evidence'))})));status.textContent=copy('assessmentSaved');saved();}
    catch(reason){status.textContent=reason instanceof Error?reason.message:String(reason);submit.disabled=false;}
  });
}
