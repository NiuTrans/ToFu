/**
 * Model-picker display folding.
 *
 * Responsibility: project a display-ordered model catalog into single,
 * alias, and version-family render units. Entry point: `modelDisplayUnits`.
 * Dependencies: none. Fold metadata remains backend-authored; this owner is
 * a pure projection and never reads browser or persistence state.
 */

export interface FoldableModelEntry {
  model_id?: string;
  fold_group?: string;
  fold_canonical?: string;
  family?: string;
  family_primary?: string;
  [key: string]: unknown;
}

export interface ModelDisplayLeaf<T extends FoldableModelEntry> {
  kind: 'single' | 'alias';
  face: T;
  members: T[];
}

export interface ModelDisplayFamily<T extends FoldableModelEntry> {
  kind: 'family';
  face: T;
  members: T[];
  children: ModelDisplayLeaf<T>[];
}

export type ModelDisplayUnit<T extends FoldableModelEntry = FoldableModelEntry> =
  ModelDisplayLeaf<T> | ModelDisplayFamily<T>;

function metadata(
  entry: FoldableModelEntry | null | undefined,
  field: keyof FoldableModelEntry,
): string {
  const value = entry && entry[field];
  return typeof value === 'string' ? value : '';
}

/** Fold a display-ordered catalog without mutating its entries or array. */
export function modelDisplayUnits<T extends FoldableModelEntry>(
  models: readonly T[] | null | undefined,
): ModelDisplayUnit<T>[] {
  const source = Array.isArray(models) ? models : [];

  // Pass one: interchangeable wire aliases become one leaf unit.
  const membersByFold = new Map<string, T[]>();
  for (const model of source) {
    const foldGroup = metadata(model, 'fold_group');
    if (!foldGroup) continue;
    const members = membersByFold.get(foldGroup) ?? [];
    members.push(model);
    membersByFold.set(foldGroup, members);
  }

  const claimedFolds = new Set<string>();
  const leaves: ModelDisplayLeaf<T>[] = [];
  for (const model of source) {
    const foldGroup = metadata(model, 'fold_group');
    const foldMembers = foldGroup ? membersByFold.get(foldGroup) : undefined;
    if (!foldGroup || !foldMembers || foldMembers.length < 2) {
      leaves.push({ kind: 'single', face: model, members: [model] });
      continue;
    }
    if (claimedFolds.has(foldGroup)) continue;
    claimedFolds.add(foldGroup);
    const canonicalId = metadata(model, 'fold_canonical');
    const face = foldMembers.find((entry) => (
      metadata(entry, 'model_id') === canonicalId
    )) ?? foldMembers[0];
    leaves.push({ kind: 'alias', face, members: foldMembers });
  }

  // Pass two: version families fold over leaves, preserving alias children.
  const childrenByFamily = new Map<string, ModelDisplayLeaf<T>[]>();
  for (const leaf of leaves) {
    const family = metadata(leaf.face, 'family');
    if (!family) continue;
    const children = childrenByFamily.get(family) ?? [];
    children.push(leaf);
    childrenByFamily.set(family, children);
  }

  const claimedFamilies = new Set<string>();
  const units: ModelDisplayUnit<T>[] = [];
  for (const leaf of leaves) {
    const family = metadata(leaf.face, 'family');
    const children = family ? childrenByFamily.get(family) : undefined;
    if (!family || !children || children.length < 2) {
      units.push(leaf);
      continue;
    }
    if (claimedFamilies.has(family)) continue;
    claimedFamilies.add(family);
    const primaryId = metadata(leaf.face, 'family_primary');
    const faceChild = children.find((child) => (
      metadata(child.face, 'model_id') === primaryId
    )) ?? children[0];
    units.push({
      kind: 'family',
      face: faceChild.face,
      members: children.flatMap((child) => child.members),
      children,
    });
  }
  return units;
}
