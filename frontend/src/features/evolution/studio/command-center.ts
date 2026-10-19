/** Studio briefing and mission HUD. Projects confirmed goal/journal snapshots;
 * entry: mountCommandCenter. Dependencies: shared UI, goal observation.
 * Owns no transport, timers, persisted state or execution policy.
 */
import { iconHtml } from '../../../icons';
import { createButton, createField } from '../../../ui/index';
import type { GoalObservation, StudioCopy } from '../goal-workbench';

export function mountCommandCenter(host: HTMLElement, form: HTMLFormElement, ports: {
  copy: StudioCopy; details: () => void; configure: () => void; briefing: () => void;
  inspect: (place:'plan'|'work'|'review'|'delivery') => void; dismiss: () => void;
}) {
  const owner = host.ownerDocument;
  const objective = form.querySelector<HTMLTextAreaElement>('#evo-objective')!;
  const model = form.querySelector<HTMLSelectElement>('#evo-model')!;
  const modelMessage = form.querySelector<HTMLElement>('.evo-model-message')!;
  const heading = owner.createElement('header');heading.className = 'evo-briefing-heading ui-stack';
  const eyebrow = owner.createElement('p');eyebrow.className = 'evo-mission-eyebrow';eyebrow.textContent = ports.copy('newGoal');
  const title = owner.createElement('h3');title.textContent = ports.copy('briefingTitle');
  heading.append(eyebrow, title);
  const field = createField({ label: ports.copy('briefingPrompt'), control: objective,
    controlId: objective.id, hint: ports.copy('briefingHint'), required: true });
  objective.rows = 5;
  const settings = owner.createElement('details');settings.className = 'evo-briefing-settings';
  const summary = owner.createElement('summary');summary.textContent = ports.copy('briefingSettings');
  const modelField = createField({ label: ports.copy('model'), control: model, controlId: model.id });
  settings.append(summary, modelField.el, modelMessage,
    createButton({label: ports.copy('roleModelTitle'), variant: 'ghost', size: 'sm', onClick: ports.configure}, owner));
  const submit = createButton({label: ports.copy('briefingDispatch'), type: 'submit', variant: 'primary', size: 'lg'}, owner);
  const dispatch = owner.createElement('div');dispatch.className = 'evo-briefing-dispatch';dispatch.append(submit);
  const back=createButton({label:ports.copy('briefingReturn'),variant:'ghost',onClick:ports.dismiss},owner);
  dispatch.append(back);
  const intro=owner.createElement('p');intro.className='evo-briefing-intro';intro.textContent=ports.copy('entryPromise');
  const examples=owner.createElement('div');examples.className='evo-briefing-examples ui-grid';
  examples.setAttribute('role','group');examples.setAttribute('aria-label',ports.copy('exampleTasks'));
  const exampleButtons=['research','benchmark','build'].map(key=>{
    const button=createButton({label:ports.copy('example_'+key),variant:'secondary',size:'sm',wrap:true,onClick:()=>{
      // Fill only an empty draft. Selecting a prompt never dispatches a task.
      if(objective.value.trim())return;
      objective.value=ports.copy('prompt_'+key);objective.dispatchEvent(new owner.defaultView!.Event('input',{bubbles:true}));objective.focus();
    }},owner);button.dataset.example=key;
    const icon=owner.createElement('span');icon.className='evo-example-icon';icon.setAttribute('aria-hidden','true');icon.innerHTML=iconHtml(({research:'search',benchmark:'chart',build:'wrench'} as Record<string,string>)[key]!);
    button.prepend(icon);examples.append(button);return button;
  });
  const refreshExamples=()=>{for(const button of exampleButtons)button.disabled=!!objective.value.trim();};
  objective.addEventListener('input',refreshExamples);refreshExamples();
  heading.append(intro);form.replaceChildren(heading, field.el, examples, settings, dispatch);
  form.classList.add('evo-briefing');
  host.append(form);

  const hud = owner.createElement('section');hud.className = 'evo-mission-hud';
  hud.setAttribute('aria-label', ports.copy('missionRoute'));
  const route = owner.createElement('ol');route.className = 'evo-mission-route';
  const phases = ['preparing', 'executing', 'reviewing', 'completed'] as const;
  const steps = phases.map((phase, index) => {
    const step = owner.createElement('li');step.dataset.phase = phase;
    const number = owner.createElement('span');number.textContent = String(index + 1).padStart(2, '0');
    const place = (['plan','work','review','delivery'] as const)[index];
    const label = createButton({label:ports.copy('phase_'+phase),
      variant:'ghost',size:'sm',wrap:true,onClick:()=>ports.inspect(place)},owner);
    const state = owner.createElement('small');
    step.append(number, label, state);route.append(step);
    return {step, state, phase};
  });
  const journal = owner.createElement('div');journal.className = 'evo-mission-signal';
  const signal = owner.createElement('p');signal.setAttribute('role', 'status');
  let current: GoalObservation = {};
  const record = createButton({label: ports.copy('activityRecord'), variant: 'ghost', size: 'sm',
    onClick: () => current.run ? ports.details() : ports.briefing()}, owner);
  journal.append(signal, record);hud.append(route, journal);
  return { hud, submit, settings,
    update(observation: GoalObservation, briefing: boolean) {
      current = observation;refreshExamples();
      const {run, questions, loading} = observation;
      form.hidden = !briefing;
      hud.hidden = briefing;
      back.hidden = !run;
      hud.dataset.state = questions ? 'waiting' : run?.status || 'idle';
      for (const {step, state, phase} of steps) {
        const current = !!run && (run.status === phase || phase === 'preparing' && ['created', 'planning'].includes(run.status));
        const recorded = !!run?.receipts?.some(receipt => receipt.stage === phase);
        step.dataset.state = current ? 'current' : recorded ? 'recorded' : 'pending';
        if (current) step.setAttribute('aria-current', 'step');else step.removeAttribute('aria-current');
        state.textContent = ports.copy(current ? questions ? 'askHuman' : phase === 'completed' ? 'completed' : 'floorLive' : recorded ? 'recorded' : 'phasePending');
      }
      // The panel already shows the latest journal text. Keep only an explicit
      // human-wait notice here, rather than repeating the same update twice.
      journal.hidden = !questions;
      signal.textContent = questions ? ports.copy('askNote') : '';
      record.textContent = ports.copy(run ? 'activityRecord' : 'newGoal');record.disabled = !!loading;
    },
    focus() { objective.focus({preventScroll: true});form.scrollIntoView?.({block: 'nearest'}); },
  };
}
