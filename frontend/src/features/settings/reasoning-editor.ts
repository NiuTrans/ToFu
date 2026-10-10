/** Model/Offering reasoning form. Edits the existing Settings draft through
 * an injected stage port; protocol compilation and persistence stay server-owned. */
import { t } from '../../i18n';
import type { ModelRoutingInferenceProfile, ModelRoutingDocument } from '../../api/api-v1-model-routing.generated';

type Profile = ModelRoutingInferenceProfile;
type Reasoning = NonNullable<Profile['reasoning']>;

export function createReasoningEditor(ports: {
  profile?: Profile; stage: (profile: Profile) => void; reset?: () => Profile | void;
}): HTMLElement {
  const root = document.createElement('details');
  root.className = 'stg-reasoning-editor';
  const summary = document.createElement('summary');
  summary.textContent = t('modelReasoning.title');
  root.append(summary);
  let built = false;
  root.addEventListener('toggle', () => {
    if (!root.open || built) return;
    built = true;
    let profile = ports.profile ?? {};
    let reasoning = profile.reasoning ?? {};
    const fields = document.createElement('div');
    fields.className = 'stg-reasoning-fields';
    function field(label: string, input: HTMLInputElement | HTMLSelectElement, name: string) {
      const wrapper = document.createElement('label');
      wrapper.textContent = label;
      input.name = name;
      wrapper.append(input);
      fields.append(wrapper);
      return input;
    }
    function select(label: string, name: string, values: Array<[string, string]>, value: string) {
      const input = document.createElement('select');
      for (const [id, text] of values) input.add(new Option(text, id));
      input.value = value;
      field(label, input, name);
      return input;
    }
    function text(label: string, name: string, value: string) {
      const input = document.createElement('input');
      input.value = value;
      input.maxLength = 2048;
      field(label, input, name);
      return input;
    }
    const support = select(t('modelReasoning.support'), 'support', [
      ['unknown', t('modelReasoning.unknown')], ['supported', t('modelReasoning.supported')],
      ['unsupported', t('modelReasoning.unsupported')]], reasoning.support ?? 'unknown');
    const efforts = text(t('modelReasoning.efforts'), 'efforts', (reasoning.supported_efforts ?? []).join(', '));
    const defaultEffort = text(t('modelReasoning.default'), 'defaultEffort', reasoning.default_effort ?? '');
    const disable = select(t('modelReasoning.disable'), 'canDisable', [
      ['unknown', t('modelReasoning.unknown')], ['false', t('modelReasoning.cannotDisable')],
      ['true', t('modelReasoning.canDisable')]], String(reasoning.can_disable ?? 'unknown'));
    const wires = ['reasoning_effort', 'thinking_type', 'enable_thinking', 'chat_template_kwargs', 'reasoning_split', 'none'];
    if (reasoning.wire_format && !wires.includes(reasoning.wire_format)) wires.push(reasoning.wire_format);
    const wire = select(t('modelReasoning.wire'), 'wireFormat', wires.map(v => [v, v]), reasoning.wire_format ?? 'reasoning_effort');
    const hint = document.createElement('p');
    hint.textContent = t('modelReasoning.hint');
    const status = document.createElement('p');
    status.setAttribute('role', 'status');
    const apply = document.createElement('button');
    apply.type = 'button';
    apply.textContent = t('modelReasoning.apply');
    apply.addEventListener('click', () => {
      try {
        const levels = efforts.value.split(/[\s,，]+/).filter(Boolean);
        if (levels.length > 32 || new Set(levels).size !== levels.length
          || levels.some(v => !/^[A-Za-z0-9_.-]{1,64}$/.test(v) || ['default', 'off', 'enabled', 'lowest'].includes(v))) {
          throw new Error(t('modelReasoning.invalidEfforts'));
        }
        const chosenDefault = defaultEffort.value.trim();
        if (chosenDefault && (!levels.includes(chosenDefault) || chosenDefault === 'none')) {
          throw new Error(t('modelReasoning.invalidDefault'));
        }
        const next: Reasoning = support.value === 'supported'
          ? { ...reasoning, support: 'supported', wire_format: wire.value,
            supported_efforts: levels, can_disable: disable.value === 'unknown' ? null : disable.value === 'true' }
          : { support: support.value as 'unsupported' | 'unknown' };
        if (support.value === 'supported') {
          if (chosenDefault) next.default_effort = chosenDefault;
          else delete next.default_effort;
          if (next.disabled_effort && !levels.includes(next.disabled_effort)) delete next.disabled_effort;
          if (next.can_disable && next.wire_format === 'reasoning_effort'
            && !levels.includes('none') && !next.disabled_effort) throw new Error(t('modelReasoning.invalidOff'));
        }
        ports.stage({ ...profile, reasoning: next, source: 'configured' });
        status.textContent = t('modelReasoning.staged');
      } catch (error) { status.textContent = String((error as Error).message); }
    });
    root.append(fields, hint, apply, status);
    if (ports.reset) {
      const reset = document.createElement('button');
      reset.type = 'button'; reset.textContent = t('modelReasoning.inherit');
      reset.addEventListener('click', () => {
        const inherited = ports.reset?.();
        if (inherited) {
          profile = inherited; reasoning = inherited.reasoning ?? {};
          support.value = reasoning.support ?? 'unknown';
          efforts.value = (reasoning.supported_efforts ?? []).join(', ');
          defaultEffort.value = reasoning.default_effort ?? '';
          disable.value = String(reasoning.can_disable ?? 'unknown');
          wire.value = reasoning.wire_format ?? 'reasoning_effort';
        }
        status.textContent = t('modelReasoning.staged');
      });
      root.append(reset);
    }
  });
  return root;
}

/** Provider overrides share the same draft owner and form as Model settings. */
export function mountProviderReasoningEditors(root: HTMLElement, getDraft: () => ModelRoutingDocument): void {
  const draft = getDraft();
  const accesses = new Map(draft.provider_accesses.map(a => [a.provider_access_id, a.provider_id]));
  const models = new Map(draft.models.map(m => [`${m.creator_id}::${m.model_id}`, m]));
  for (const card of root.querySelectorAll<HTMLElement>('.stg-provider-card-v2')) {
    const offerings = draft.offerings.filter(o => accesses.get(o.provider_access_id) === card.dataset.providerId);
    if (!offerings.length) continue;
    const group = document.createElement('details');
    group.className = 'stg-reasoning-editor';
    const summary = document.createElement('summary');
    summary.textContent = t('modelReasoning.providerOverrides');
    group.append(summary);
    let built = false;
    group.addEventListener('toggle', () => {
      if (!group.open || built) return;
      built = true;
      for (const offering of offerings) {
        const model = offering.model ? models.get(`${offering.model.creator_id}::${offering.model.model_id}`) : undefined;
        const base = model?.inference_profile ?? {};
        const own = offering.inference_profile ?? {};
        const effective = { ...base, ...own, reasoning: { ...base.reasoning, ...own.reasoning } };
        // A successful CAS replaces the Settings draft. Resolve the owner row
        // at each edit so a second save/reset never mutates a detached object.
        function currentOwner() {
          const current = getDraft();
          const row = current.offerings.find(o => o.offering_id === offering.offering_id);
          const ownerModel = current.models.find(m => m.creator_id === row?.model?.creator_id
            && m.model_id === row?.model?.model_id);
          return { row, base: ownerModel?.inference_profile ?? {} };
        }
        const editor = createReasoningEditor({ profile: effective,
          stage: profile => {
            const { row, base: currentBase } = currentOwner();
            if (!row) return;
            // Store only differing fields; unchanged Model facts keep inheriting.
            const overrides: Record<string, unknown> = {};
            for (const [key, value] of Object.entries(profile.reasoning ?? {})) {
              if (JSON.stringify(value) !== JSON.stringify((currentBase.reasoning as Record<string, unknown> | undefined)?.[key])) overrides[key] = value;
            }
            row.inference_profile = { ...row.inference_profile, reasoning: overrides as Reasoning };
          },
          reset: () => {
            const { row, base: currentBase } = currentOwner();
            if (row?.inference_profile) delete row.inference_profile.reasoning;
            return currentBase;
          },
        });
        editor.dataset.offeringId = offering.offering_id;
        const title = editor.querySelector('summary');
        if (title) title.textContent = model?.display_name || model?.model_id || offering.pending_model_id || offering.offering_id;
        group.append(editor);
      }
    });
    card.querySelector('.stg-provider-body')?.append(group);
  }
}
