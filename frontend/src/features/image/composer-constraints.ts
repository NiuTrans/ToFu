/**
 * Image composer options a selected Model documents.
 *
 * Responsibility: decide which aspect-ratio and resolution options the
 * creative image composer offers for one routed Model, from that Model's
 * media_profile (registered or maintained official facts, delivered on
 * /api/v4/images/models). Pure; the retained image-generation runtime owns
 * the pills and applies the result. A fact's absence never disables a choice:
 * undocumented constraints, "All Models" and profiles sharing no value with
 * the offered pills keep every option.
 */

export interface ImageComposerOptions {
  readonly aspects: readonly string[];
  readonly resolutions: readonly string[];
}

function documented(value: unknown): ReadonlySet<string> | null {
  if (!Array.isArray(value)) return null;
  const values = value.filter((item): item is string => typeof item === 'string' && item.length > 0);
  return values.length ? new Set(values) : null;
}

function narrowed(offered: readonly string[], allowed: ReadonlySet<string> | null): string[] {
  if (!allowed) return [...offered];
  const kept = offered.filter((value) => allowed.has(value));
  // The pills cannot express this Model's own values; offering nothing would
  // strand the composer, so the choice stays with the user and provider.
  return kept.length ? kept : [...offered];
}

/** The subset of offered options the Model's media profile supports. */
export function supportedImageComposerOptions(
  profile: unknown,
  offered: ImageComposerOptions,
): ImageComposerOptions {
  const media = profile && typeof profile === 'object' ? profile as Record<string, unknown> : {};
  return {
    aspects: narrowed(offered.aspects, documented(media.aspect_ratios)),
    resolutions: narrowed(offered.resolutions, documented(media.resolutions)),
  };
}
