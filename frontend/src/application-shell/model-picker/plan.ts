/** Display grouping, ordering, recents and fold planning. Map keys accept arbitrary canonical IDs; no DOM or persistence. */
import type { ModelDisplayUnit } from '../../core/model-display-fold';
import { isCurrentModel, type ModelPickerPorts, type PickerModel } from './types';
export interface PickerGroup { key: string; name: string; models: PickerModel[]; units: ModelDisplayUnit<PickerModel>[] }
export function planModelPicker(models: readonly PickerModel[], ports: ModelPickerPorts) {
  const selection = ports.selection(), hidden = ports.hiddenModels();
  const visible = models.filter(model => isCurrentModel(model, selection) || (!hidden.has(model.model_id) && ports.isChatModel(model)));
  const groups = new Map<string, PickerGroup>(), firstById = new Map<string, PickerModel>();
  for (const model of visible) {
    if (!firstById.has(model.model_id)) firstById.set(model.model_id, model);
    const policyKey = ports.groupKey(model);
    const key = JSON.stringify([model.routing_v2 ? (model.pending_identity ? 'pending' : 'creator') : 'legacy', policyKey]);
    let group = groups.get(key);
    if (!group) { group = { key, name: ports.groupLabel(policyKey, model), models: [], units: [] }; groups.set(key, group); }
    group.models.push(model);
  }
  const ordered = [...groups.values()];
  if (ports.compare) ordered.sort((a, b) => ports.compare!(a.name || a.key, b.name || b.key));
  for (const group of ordered) {
    if (ports.compare) group.models.sort(ports.compare);
    group.units = ports.fold && !group.models.some(model => model.routing_v2)
      ? ports.fold(group.models) : group.models.map(model => ({ kind: 'single', face: model, members: [model] }));
  }
  const recents = ports.fold && visible.length > 8
    ? ports.recentIds().filter(id => firstById.has(id)).map(id => firstById.get(id)!) : [];
  return { visible, groups: ordered, recents };
}
