/** Provider wire-name planning and editing. Model identity never changes here.
 * Entry points are mounted by Settings; the existing owner draft/CAS owns writes. */
import type { ModelRoutingDocument } from '../../api/api-v1-model-routing.generated';
import type { ProviderTemplate } from './provider-template-picker';
import { createButton } from '../../ui';
import { t } from '../../i18n';

export function suggestedRequestNames(modelId: string, urls: string[], templates: ProviderTemplate[]): string[] {
  const endpoints = new Set(urls.map(url => url.replace(/\/+$/, '')));
  const matches = templates.filter(template => endpoints.has((template.base_url ?? '').replace(/\/+$/, '')));
  const names = matches.flatMap(template => (template.offering_recipes ?? [])
    .filter(recipe => recipe.model_id === modelId)
    .flatMap(recipe => recipe.request_ids?.length ? recipe.request_ids : [modelId]));
  return [...new Set(names.length ? names : [modelId])];
}

export function renameRequestName(document: ModelRoutingDocument, deploymentId: string, name: string): void {
  const row = document.deployments.find(row => row.deployment_id === deploymentId);
  if (!row) return;
  name = name.trim();
  if (!name || name.length > 256 || /[\u0000-\u001f]/.test(name)) throw new Error(t('settings.requestNameInvalid'));
  const offering = document.offerings.find(item => item.offering_id === row.offering_id);
  const siblings = new Set(document.offerings.filter(item => item.provider_access_id === offering?.provider_access_id)
    .map(item => item.offering_id));
  if (document.deployments.some(item => item !== row && siblings.has(item.offering_id) && item.wire_model_id === name)) {
    throw new Error(t('settings.requestNameDuplicate'));
  }
  const old = row.wire_model_id;
  if (old === name) return;
  for (const credential of document.credentials.filter(item => item.provider_access_id === offering?.provider_access_id)) {
    for (const grant of credential.authorization.models) {
      if (grant.creator_id === offering?.model?.creator_id && grant.model_id === offering?.model?.model_id
          && grant.excluded_wire_model_ids?.includes(old)) {
        grant.excluded_wire_model_ids = [...new Set(grant.excluded_wire_model_ids.map(id => id === old ? name : id))];
      }
    }
  }
  row.wire_model_id = name;
  row.probe_status = 'unprobed';
}

export function mountRequestNameEditors(root: HTMLElement, ports: {
  document: () => ModelRoutingDocument;
  prompt: (name: string) => Promise<string | null>;
  alert: (message: string) => void;
  render: () => void;
}): void {
  const document = ports.document();
  for (const header of root.querySelectorAll<HTMLElement>('.is-logical[data-offering]')) {
    const offering = document.offerings[Number(header.dataset.offering)];
    const model = document.models.find(row => row.creator_id === offering?.model?.creator_id && row.model_id === offering?.model?.model_id);
    if (model?.registration_state && model.registration_state !== 'ready') {
      const chip = root.ownerDocument.createElement('span');
      chip.className = 'stg-mx-chip pending';
      chip.textContent = t(model.registration_state === 'disabled' ? 'modelCatalog.registrationDisabled' : 'modelCatalog.registrationPending');
      chip.title = (model.registration_issues ?? []).join('; ');
      header.querySelector('.stg-mx-model')?.append(chip);
    }
  }
  for (const host of root.querySelectorAll<HTMLElement>('[data-request-deployment]')) {
    const id = host.dataset.requestDeployment!;
    const row = ports.document().deployments.find(row => row.deployment_id === id);
    if (!row) continue;
    const button = createButton({ label: row.wire_model_id, title: t('settings.requestNameEdit'), variant: 'ghost', size: 'sm',
      onClick: async () => {
        const draft = ports.document();
        const old = row.wire_model_id;
        const name = await ports.prompt(old);
        if (name === null || draft !== ports.document() || row.wire_model_id !== old) return;
        try { renameRequestName(draft, id, name); ports.render(); }
        catch (error) { ports.alert(String((error as Error).message)); }
      } });
    host.replaceChildren(button);
    if (row.registration_issues?.length) {
      const chip = root.ownerDocument.createElement('span');
      chip.className = 'stg-mx-chip pending';
      chip.textContent = t('modelCatalog.registrationPending');
      chip.title = row.registration_issues.join('; ');
      host.append(chip);
    }
  }
}
