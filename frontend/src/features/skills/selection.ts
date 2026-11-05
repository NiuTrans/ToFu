/** Explicit skill selection over the shared installed catalog. No installation,
 * activation, background polling or execution; the server binds guide versions. */
import {createButton, createField} from '../../ui';
import type {InstalledSkillOption} from '../../api/endpoint-registry/skills';

export function mountSkillSelection(host: HTMLElement, ports: {
  copy: (key: string) => string;
  project: () => string;
  owner?: () => unknown;
  list?: (project: string, signal: AbortSignal) => Promise<{skills: InstalledSkillOption[]}>;
}) {
  const {copy} = ports;
  const control = document.createElement('textarea');
  control.name = 'selected-skills'; control.rows = 2; control.maxLength = 2064; control.className = 'ui-input';
  host.append(createField({label: copy('skills'), hint: copy('skillsHint'), control, controlId: 'exp-selected-skills'}).el);
  const options = document.createElement('select'); options.className = 'ui-input';
  options.dataset.skillOptions = 'true';
  options.setAttribute('aria-label', copy('availableSkills'));
  options.append(new Option(copy('availableSkills'), ''));
  const status = document.createElement('p'); status.className = 'exp-muted'; status.setAttribute('role', 'status');
  const row = document.createElement('div'); row.className = 'ui-row';
  let disposed = false, epoch = 0, loadedProject = '', loadedOwner: unknown, abort: AbortController | undefined;
  const refresh = createButton({label: copy('loadSkills'), size: 'sm', disabled: !ports.list, onClick: () => void load()});
  refresh.dataset.loadSkills = 'true';
  function reset() {
    epoch += 1; abort?.abort(); abort = undefined; loadedProject = '';
    options.replaceChildren(new Option(copy('availableSkills'), ''));
    refresh.disabled = !ports.list; status.textContent = '';
  }
  async function load() {
    if (!ports.list || disposed) return;
    reset(); abort = new AbortController(); const captured = epoch, project = ports.project(), owner = ports.owner?.();
    refresh.disabled = true; status.textContent = copy('skillsLoading');
    try {
      const result = await ports.list(project, abort.signal);
      if (disposed || captured !== epoch || project !== ports.project() || owner !== ports.owner?.()) return;
      if (!Array.isArray(result.skills)) throw Error(copy('skillsUnavailable'));
      loadedProject = project; loadedOwner = owner;
      for (const skill of result.skills) {
        const item = new Option(`${skill.name || skill.id} (${skill.id})`, skill.id);
        item.disabled = !skill.enabled || !skill.eligible;
        item.title = (skill.ineligible_reasons || []).join('; '); options.append(item);
      }
      status.textContent = result.skills.length ? copy('skillsChoose') : copy('skillsEmpty');
    } catch (reason) {
      if (!disposed && captured === epoch) status.textContent = copy('skillsUnavailable') + ': ' + (reason instanceof Error ? reason.message : String(reason));
    } finally {if (!disposed && captured === epoch) refresh.disabled = !ports.list;}
  }
  options.addEventListener('change', () => {
    if (disposed) return;
    if (!options.value || loadedProject !== ports.project() || loadedOwner !== ports.owner?.()) {reset(); return;}
    control.value = [...new Set([...control.value.split(/\s+/).filter(Boolean), options.value])].join('\n');
    options.value = '';
    control.dispatchEvent(new control.ownerDocument.defaultView!.Event('input', {bubbles: true}));
  });
  row.append(refresh, options); host.append(row, status);
  return {
    control, reset,
    read(): string[] {
      const ids = control.value.split(/\s+/).filter(Boolean).map(value => value.replace(/^\$/, ''));
      if (ids.length > 16 || new Set(ids).size !== ids.length || ids.some(id => !/^[a-zA-Z][a-zA-Z0-9_-]{0,127}$/.test(id))) throw Error(copy('skillsInvalid'));
      return ids;
    },
    dispose() {if (disposed) return; disposed = true; epoch += 1; abort?.abort(); abort = undefined;},
  };
}
