/** Model-picker display contracts. Canonical routing DTOs remain generated; browser selection is injected, never owned here. */
import type { FoldableModelEntry, ModelDisplayUnit } from '../../core/model-display-fold';
export interface ProviderOption { provider_id: string; provider_name: string; offering_id?: string; brand?: string }
export interface PickerModel extends FoldableModelEntry {
  model_id: string; display_name?: string; creator_id?: string; creator_name?: string; provider_id?: string; provider_name?: string;
  offering_id?: string; brand?: string; routing_v2?: boolean; pending_identity?: boolean;
  capabilities?: readonly string[]; thinking_default?: boolean; provider_options?: ProviderOption[];
}
export interface PickerSelection {
  modelId: string; preferredProviderId: string;
  modelRef?: { creator_id?: string; model_id?: string; provider_id?: string; offering_id?: string } | null;
}
export interface ModelPickerPorts {
  document: Document; window: Window; list: HTMLElement;
  selection(): PickerSelection; hiddenModels(): ReadonlySet<string>; isChatModel(model: PickerModel): boolean;
  groupKey(model: PickerModel): string; groupLabel(key: string, model: PickerModel): string;
  compare?: (left: PickerModel | string, right: PickerModel | string) => number;
  fold?: (models: PickerModel[]) => ModelDisplayUnit<PickerModel>[];
  recentIds(): readonly string[]; shortName(id: string): string;
  brand(model: PickerModel): string; icon(brand: string, size: number): string;
  text(key: string, params?: Record<string, unknown>): string;
  select(model: PickerModel): unknown; openSettings(): void;
}
export function isCurrentModel(model: PickerModel, selection: PickerSelection): boolean {
  if (model.model_id !== selection.modelId) return false;
  if (!model.routing_v2) return true;
  const ref = selection.modelRef;
  if (model.pending_identity) return (model.provider_id || '') === selection.preferredProviderId
    && (!ref?.offering_id || ref.offering_id === model.offering_id);
  return (!ref?.creator_id || ref.creator_id === model.creator_id) && (!ref?.model_id || ref.model_id === model.model_id);
}
export function preferredProvider(model: PickerModel, selection: PickerSelection): string {
  return isCurrentModel(model, selection) ? selection.preferredProviderId : '';
}
