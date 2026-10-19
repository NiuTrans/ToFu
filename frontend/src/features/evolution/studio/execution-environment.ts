/** Goal execution environment intake. Reads the actor capability catalog once;
 * returns explicit launch fields. No credentials, task dispatch, timers or cache.
 * Entry: mountExecutionEnvironment; dependencies: UI kit and run API port.
 */
import { createButton, createField } from '../../../ui/index';
import type { CreateRunBody } from '../../../api/evolution-runs';
type Catalog = {extensibility?: {plugins?: {available?: Record<string, string[]>}}};
export function mountExecutionEnvironment(host: HTMLElement, ports: {
  copy: (key: string) => string; load: (signal: AbortSignal) => Promise<Catalog>; signal: AbortSignal;
}) {
  const projectField = createField({label: ports.copy('projectDirectory'), inputType: 'text', controlId: 'evo-project-path'});
  const project = projectField.control as HTMLInputElement;project.name = 'project_path';
  const environmentField = createField({label: ports.copy('executionEnvironment'),
    choices: [{label: ports.copy('environmentInherit'), value: ''}], controlId: 'evo-execution-environment', hint: ports.copy('environmentHint')});
  const select = environmentField.control as HTMLSelectElement;select.name = 'executor_plugins';
  const status = document.createElement('p');status.setAttribute('role', 'status');
  let busy = false, loading = false;
  const retry = createButton({label: ports.copy('environmentRetry'), variant: 'ghost', size: 'sm', onClick: () => {void load();}});
  retry.hidden = true;
  host.append(projectField.el, environmentField.el, status, retry);
  function disabled() {project.disabled = busy;select.disabled = busy || loading;retry.disabled = busy || loading;}
  async function load() {
    if (loading || ports.signal.aborted) return;
    loading = true;disabled();status.textContent = ports.copy('environmentLoading');retry.hidden = true;
    try {
      const catalog = await ports.load(ports.signal);
      if (ports.signal.aborted) return;
      const saved = select.value;
      select.replaceChildren(new Option(ports.copy('environmentInherit'), ''), new Option(ports.copy('environmentNone'), '[]'));
      const names = Object.keys(catalog.extensibility?.plugins?.available || {}).sort();
      names.forEach(name => select.append(new Option(name, JSON.stringify([name]))));
      if (saved && !Array.from(select.options).some(option => option.value === saved)) {
        const missing = new Option(ports.copy('environmentUnavailable'), saved);missing.disabled = true;select.append(missing);
      }
      select.value = saved;status.textContent = ports.copy(names.length ? 'environmentReady' : 'environmentEmpty');
    } catch {
      if (!ports.signal.aborted) {status.textContent = ports.copy('environmentUnavailable');retry.hidden = false;}
    } finally {loading = false;disabled();}
  }
  void load();
  return {
    setBusy(value: boolean) {busy = value;disabled();},
    read(): Pick<CreateRunBody, 'project_path' | 'executor_plugins'> {
      if (select.selectedOptions[0]?.disabled) throw Error(ports.copy('environmentUnavailable'));
      return {...(project.value.trim() ? {project_path: project.value.trim()} : {}),
        ...(select.value ? {executor_plugins: JSON.parse(select.value) as string[]} : {})};
    },
  };
}
