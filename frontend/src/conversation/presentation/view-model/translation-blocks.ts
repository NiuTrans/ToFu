/**
 * Authoritative proposed-plan translation and preview selection. No task recovery,
 * polling, DOM or mutation.
 * Entry: withoutAuthoritativeProposedPlan, translatedProposedPlanBody,
 * translatedPlanPreview.
 * Dependencies: conversation-sync.generated, types. */
import {
  type TurnProjection,
  type TurnProposedPlan,
} from '../../../api/conversation-sync.generated';
import {
  type TranslationActivity,
} from './types';

export function withoutAuthoritativeProposedPlan(
  markdown: string,
  expectedText?: string,
): string {
  const pattern = /<proposed_plan>\s*\n?([\s\S]*?)\n?\s*<\/proposed_plan>/gi;
  let selected: { index: number; length: number } | null = null;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(markdown)) !== null) {
    if (expectedText === undefined || match[1].trim() === expectedText.trim()) {
      selected = { index: match.index, length: match[0].length };
    }
  }
  if (!selected) return markdown.trimEnd();
  const before = markdown.slice(0, selected.index);
  const after = markdown.slice(selected.index + selected.length);
  let visible = `${before}${after}`;
  if (!before.trim()) {
    visible = visible.replace(/^(?:[ \t]*\r?\n)+/, '');
  }
  return visible.replace(/\n[ \t]*\n[ \t]*\n+/g, '\n\n').trimEnd();
}

/**
 * Read the last translated plan body without granting it execution authority.
 * Completed projections require the closing tag. Lifecycle-local previews may
 * expose the body after the opening tag while the translator is still writing.
 */
export function translatedProposedPlanBody(
  markdown: string | undefined,
  allowIncomplete: boolean,
): string | undefined {
  if (!markdown) return undefined;
  const openPattern = /<proposed_plan>\s*\n?/gi;
  let open: RegExpExecArray | null = null;
  let candidate: RegExpExecArray | null;
  while ((candidate = openPattern.exec(markdown)) !== null) open = candidate;
  if (!open) return undefined;
  const tail = markdown.slice(open.index + open[0].length);
  const close = tail.search(/\n?\s*<\/proposed_plan>/i);
  if (close < 0 && !allowIncomplete) return undefined;
  const body = (close < 0 ? tail : tail.slice(0, close))
    .replace(/\n?\s*<\/?proposed_plan[^>]*$/i, '');
  const normalized = body.trim();
  return normalized || undefined;
}

function untaggedPlanTranslation(
  markdown: string | undefined,
): string | undefined {
  if (!markdown || /<\/?proposed_plan\b/i.test(markdown)) return undefined;
  return markdown.trim() || undefined;
}

export function translatedPlanPreview(
  projection: TurnProjection,
  proposedPlan: TurnProposedPlan,
  activity?: TranslationActivity,
): { markdown?: string; pending: boolean; streaming: boolean } {
  const pending = activity?.status === 'pending';
  const originalOutsidePlan = withoutAuthoritativeProposedPlan(
    projection.content ?? '', proposedPlan.text,
  ).trim();
  const planOnly = originalOutsidePlan.length === 0;
  const completed = translatedProposedPlanBody(
    projection.translatedContent, false,
  ) ?? (planOnly
    ? untaggedPlanTranslation(projection.translatedContent) : undefined);
  const partial = pending
    ? translatedProposedPlanBody(activity?.partial, true)
      ?? (planOnly ? untaggedPlanTranslation(activity?.partial) : undefined)
    : undefined;
  // A finished translation outranks an in-flight preview, matching every other
  // block kind (`visibleTranslation ?? visiblePreview`). Preferring the partial
  // made a re-run task swallow prose the reader already had and stream it back
  // from the first chunk.
  return {
    markdown: completed ?? partial,
    pending,
    streaming: Boolean(partial) && completed === undefined,
  };
}
