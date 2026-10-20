/** Owner-routing aggregate → model-first display rows. One capability union per model; pending offerings remain provider-scoped. */
import type { ModelRoutingDocument } from '../../api/api-v1-model-routing.generated';
import type { PickerModel } from './types';
export function projectModelPickerCatalog(value: ModelRoutingDocument | null | undefined): PickerModel[] {
  if (!value || value.contract_version !== 'tofu.model-routing/v2') return [];
  const providers = new Map((value.providers || []).map(row => [row.provider_id, row]));
  const accesses = new Map((value.provider_accesses || []).map(row => [row.provider_access_id, row]));
  const creators = new Map((value.creators || []).map(row => [row.creator_id, row]));
  const identity = (creator: string, model: string) => JSON.stringify([creator, model]);
  const models = new Map((value.models || []).map(row => [identity(row.creator_id, row.model_id), row]));
  const supplied = new Set((value.deployments || []).map(row => row.offering_id));
  const pending: PickerModel[] = [];
  const automatic = new Map<string, { row: PickerModel; capabilities: Set<string> }>();
  for (const offering of value.offerings || []) {
    const access = accesses.get(offering.provider_access_id);
    const provider = access && providers.get(access.provider_id);
    if (!provider || !access?.enabled || !offering.enabled || offering.stale || !supplied.has(offering.offering_id)) continue;
    const ref = offering.identity_state === 'confirmed' && offering.model;
    const model = ref ? models.get(identity(ref.creator_id, ref.model_id)) : null;
    const name = access.display_name || provider.name || provider.provider_id;
    if (!model) {
      pending.push({ model_id: offering.pending_model_id || '', creator_id: '', creator_name: '',
        offering_id: offering.offering_id, provider_id: provider.provider_id, provider_name: name,
        capabilities: offering.capabilities || [], thinking_default: (offering.capabilities || []).includes('thinking'),
        brand: provider.brand || '', routing_v2: true, pending_identity: true });
      continue;
    }
    const key = identity(model.creator_id, model.model_id);
    let entry = automatic.get(key);
    if (!entry) {
      const creator = creators.get(model.creator_id);
      entry = { capabilities: new Set(), row: { model_id: model.model_id, display_name: model.display_name || '', creator_id: model.creator_id,
        creator_name: creator?.name || model.creator_id, offering_id: '', provider_id: '', provider_name: '自动服务商',
        provider_options: [], brand: model.creator_id || '', routing_v2: true, pending_identity: false } };
      automatic.set(key, entry);
    }
    for (const capability of offering.capabilities || []) entry.capabilities.add(capability);
    entry.row.provider_options!.push({ provider_id: provider.provider_id, provider_name: name,
      offering_id: offering.offering_id, brand: provider.brand || '' });
  }
  for (const { row, capabilities } of automatic.values()) {
    row.capabilities = [...capabilities].sort(); row.thinking_default = capabilities.has('thinking');
    row.provider_options!.sort((a, b) => a.provider_name.localeCompare(b.provider_name, undefined, { numeric: true, sensitivity: 'base' }));
  }
  return [...automatic.values()].map(entry => entry.row).concat(pending);
}
