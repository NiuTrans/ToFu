/**
 * Display facts for one context note, shared by the typed Surface and the
 * retained timeline so both paths render the same row: the registry label,
 * glyph and authority of its reason, and reading aids over its verbatim text.
 * Nothing here rewrites the text: every line keeps its exact bytes.
 * Entry points: contextNoteFacet, contextNoteTextRuns.
 * Dependencies: the generated registry presentation contract.
 */
import {
  SYSTEM_NOTE_DISPLAY_POLICY,
  SYSTEM_NOTE_REASON_PRESENTATION,
} from '../ui/system-note-presentation.generated';

export type ContextNoteFacet = Readonly<{
  /** Registry reason id, or '' when the note carries none. */
  reason: string;
  /** i18n key of the row title; '' renders `fallbackLabel` as is. */
  labelKey: string;
  fallbackLabel: string;
  /** Shared icon name (frontend/src/icons.ts). */
  glyph: string;
  /** Registry authority of the injected block, or '' when unknown. */
  authority: string;
  /** One-line glimpse: a rule file's source path(s), otherwise the first
   * readable line of the text. */
  peek: string;
  /** Non-blank lines; one means the whole text fits its row. */
  readableLines: number;
}>;

/** A run of verbatim text: markup wrappers and headings read differently. */
export type ContextNoteTextRun = Readonly<{
  kind: 'markup' | 'heading' | 'text';
  text: string;
}>;

const FALLBACK_GLYPH = 'layers';
const MARKUP_LINE = /^(?:<\/?[A-Za-z][\w:.-]*(?:\s[^<>]*)?\/?>|<!--.*-->)$/;
const HEADING_LINE = /^#{1,6}\s/;
// Every rule file renders the same `Project Intelligence — AGENTS.md` title;
// only its `Source:` line (or a status note's first line) names the file.
const RULE_FILE_REASON = 'project_scoped_rules';
const RULE_TITLE_PREFIX = 'Project Intelligence — ';
const RULE_SOURCE_LINE = /^Source: (.+?)(?: — .*)?$/;

function noteField(note: unknown, field: string): string {
  if (!note || typeof note !== 'object') return '';
  const value = (note as Record<string, unknown>)[field];
  return typeof value === 'string' ? value : '';
}

function ownEntry<T>(table: Readonly<Record<string, T>>, key: string): T | undefined {
  return key && Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

function isMarkupLine(line: string): boolean {
  return MARKUP_LINE.test(line.trim());
}

/** The first non-blank line that is neither a wrapper tag nor a heading,
 * without its list marker: the line a reader would quote. */
function readablePeek(lines: readonly string[]): string {
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || isMarkupLine(trimmed) || HEADING_LINE.test(trimmed)) continue;
    return trimmed.replace(/^(?:[-*+]|\d+[.)])\s+/, '');
  }
  return '';
}

/** Source paths of a rule-file note: its first line, or a line right after a
 * rule title, never a `Source:` line quoted inside the rule text itself. */
function ruleSourcePaths(lines: readonly string[]): string[] {
  const paths: string[] = [];
  lines.forEach((line, index) => {
    if (index > 0 && !lines[index - 1].startsWith(RULE_TITLE_PREFIX)) return;
    const match = RULE_SOURCE_LINE.exec(line);
    if (match) paths.push(match[1]);
  });
  return paths;
}

export function contextNoteFacet(note: unknown): ContextNoteFacet {
  const reason = noteField(note, 'reason') || noteField(note, 'injectionId');
  const sessionWitness = !reason && noteField(note, 'noteKind') === 'session-context';
  const presentation = ownEntry(
    SYSTEM_NOTE_REASON_PRESENTATION, sessionWitness ? 'session_context_committed' : reason);
  const policy = ownEntry(SYSTEM_NOTE_DISPLAY_POLICY, reason);
  const lines = noteField(note, 'text').split('\n');
  let labelKey = presentation?.labelKey ?? '';
  let fallbackLabel = presentation?.fallback ?? reason;
  if (!presentation && !reason) {
    labelKey = 'systemNote.contextInjectionLabel';
    fallbackLabel = 'Context injected';
  }
  return {
    reason,
    labelKey,
    fallbackLabel,
    glyph: presentation?.glyph || FALLBACK_GLYPH,
    authority: policy?.authority ?? (sessionWitness ? 'platform' : ''),
    peek: (reason === RULE_FILE_REASON ? ruleSourcePaths(lines) : []).join(' · ')
      || readablePeek(lines),
    readableLines: lines.filter((line) => line.trim()).length,
  };
}

/** Split verbatim text into runs whose concatenation is the exact input:
 * whole-line wrapper tags/comments and Markdown headings become their own
 * runs (newline included) so a renderer can quiet or emphasise them. */
export function contextNoteTextRuns(text: string): readonly ContextNoteTextRun[] {
  const runs: ContextNoteTextRun[] = [];
  const lines = text.split('\n');
  lines.forEach((line, index) => {
    const piece = index < lines.length - 1 ? `${line}\n` : line;
    if (!piece) return;
    const trimmed = line.trim();
    const kind = isMarkupLine(trimmed) ? 'markup'
      : HEADING_LINE.test(trimmed) ? 'heading' : 'text';
    const previous = runs[runs.length - 1];
    if (kind === 'text' && previous?.kind === 'text') {
      runs[runs.length - 1] = { kind, text: previous.text + piece };
    } else {
      runs.push({ kind, text: piece });
    }
  });
  return runs;
}
