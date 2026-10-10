/** Current experiment projection. Execution identity owns links and retained disclosures. */
import type { ExplorationProgram, ExplorationStep, ReplanProposal } from '../../../api/evolution-programs';
import type { EvolutionRun } from '../../../api/evolution-runs';
import { createButton } from '../../../ui';

export function mountCurrentExperiment(host: HTMLElement, ports: {
  copy: (key: string) => string;
  hypothesis: (step: ExplorationStep) => HTMLDetailsElement | null;
  replan: (proposal: ReplanProposal) => HTMLDetailsElement;
  openTask: (id: string) => void;
}) {
  let signature = '', identity = '', selectedTask = '', disposed = false;
  const node = <K extends keyof HTMLElementTagNameMap>(tag: K, text = '', className = '') => {
    const element = document.createElement(tag); element.textContent = text; element.className = className; return element;
  };
  const heading = node('h2', ports.copy('current'));
  const room = node('a', ports.copy('openRoom'), 'exp-link');
  const errorDetail = node('details', '', 'exp-current-error');
  const errorSummary = node('summary', ports.copy('technical'));
  const errorText = node('pre', '', 'exp-result');
  errorDetail.append(errorSummary, errorText);
  const inspect = createButton({label: ports.copy('execution'), size: 'sm', onClick: () => {
    if (!disposed && selectedTask) ports.openTask(selectedTask);
  }});
  return {
    update(program?: ExplorationProgram, run?: EvolutionRun, runError?: string) {
      if (disposed) return;
      if (!program) { signature = identity = selectedTask = ''; host.replaceChildren(); return; }
      const current = program.current;
      // Only the current experiment's confirmed run may project status or usage.
      run = current && run?.run_id === current.run_id ? run : undefined;
      const nextIdentity = JSON.stringify([program.program_id, current?.run_id, current?.sequence]);
      const nextSignature = JSON.stringify([nextIdentity, program.status, current?.step, program.replan_proposal,
        run?.status, run?.task_id, run?.actuals_complete, run?.error, runError]);
      if (signature === nextSignature) return;
      const focus = host.contains(document.activeElement) ? document.activeElement as HTMLElement : null;
      const expanded = nextIdentity === identity && host.querySelector<HTMLDetailsElement>('.exp-hypothesis')?.open;
      const errorExpanded = nextIdentity === identity && host.querySelector<HTMLDetailsElement>('.exp-current-error')?.open;
      signature = nextSignature; identity = nextIdentity;
      selectedTask = current && run?.run_id === current.run_id ? run.task_id || '' : '';
      host.replaceChildren(heading);
      if (!current) { host.append(node('p', ports.copy('state.' + program.status), 'exp-muted')); return; }
      host.append(node('p', ports.copy('phase.' + current.step.kind), 'exp-phase'), node('h3', current.step.title),
        node('p', ports.copy(run ? 'run.' + run.status : runError ? 'runUnavailable' : 'preparing'), 'exp-muted'));
      if (run && runError) host.append(node('p', ports.copy('runRetained'), 'exp-muted'));
      if (run?.error) {
        errorDetail.open = !!errorExpanded;
        errorText.textContent = run.error;
        host.append(errorDetail);
      }
      const checks = node('ul', '', 'exp-checks');
      current.step.checks.forEach(check => checks.append(node('li', check)));
      host.append(node('h4', ports.copy('checks')), checks);
      const hypothesis = ports.hypothesis(current.step);
      if (hypothesis) { hypothesis.open = !!expanded; host.append(hypothesis); }
      if (current.step.recovery_of) host.append(node('p', `${ports.copy('recoveryOf')}: ${current.step.recovery_of}`, 'exp-muted'));
      if (program.replan_proposal) host.append(ports.replan(program.replan_proposal));
      if (run?.actuals_complete === false) host.append(node('p', ports.copy('usageIncomplete'), 'exp-muted'));
      room.href = `#/evolution/runs/${encodeURIComponent(current.run_id)}`; host.append(room);
      inspect.hidden = !selectedTask; host.append(inspect);
      if ((focus === room || focus === inspect || focus === errorSummary) && focus.isConnected && !focus.hidden) focus.focus({preventScroll: true});
    },
    dispose() { disposed = true; selectedTask = ''; host.replaceChildren(); },
  };
}
