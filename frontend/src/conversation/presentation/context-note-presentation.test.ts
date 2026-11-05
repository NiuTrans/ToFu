import { describe, expect, it } from 'vitest';
import { hasIcon } from '../../icons';
import { SYSTEM_NOTE_REASON_PRESENTATION } from '../ui/system-note-presentation.generated';
import { contextNoteFacet, contextNoteTextRuns } from './context-note-presentation';

describe('context note text runs', () => {
  it('reassemble the exact verbatim bytes', () => {
    for (const text of [
      '', 'one line', 'trailing newline\n', '\n\nleading blanks',
      '<available_skills>\nGuides.\n\n- a (global): x\n</available_skills>',
      '# Environment\n - cwd: /tmp\n<!-- tofu-context:x:start -->\nbody',
    ]) {
      expect(contextNoteTextRuns(text).map((run) => run.text).join('')).toBe(text);
    }
  });

  it('separate whole-line wrapper tags and headings from prose', () => {
    expect(contextNoteTextRuns('<rules>\n# Title\nkeep <b>inline</b> tags\n</rules>')).toEqual([
      { kind: 'markup', text: '<rules>\n' },
      { kind: 'heading', text: '# Title\n' },
      { kind: 'text', text: 'keep <b>inline</b> tags\n' },
      { kind: 'markup', text: '</rules>' },
    ]);
  });
});

describe('context note facet', () => {
  it('titles a known reason by its registry label, glyph and authority', () => {
    const facet = contextNoteFacet({
      reason: 'skills_index', noteKind: 'context-injection',
      text: '<available_skills>\n# Guides\n- slides: decks\n</available_skills>',
    });
    expect(facet).toMatchObject({
      labelKey: 'systemNote.reasonSkillsIndex', glyph: 'puzzle', authority: 'workflow',
      peek: 'slides: decks', readableLines: 4,
    });
  });

  it('glimpses a rule file by its source path, not its shared title', () => {
    const rule = (path: string, body: string) => [
      'Project Intelligence — AGENTS.md', `Source: ${path}`,
      `Applies only within: ${path.replace(/\/AGENTS\.md$/, '')}`,
      'Nested AGENTS.md specializes its subtree; guidance does not grant access.', '', body,
    ].join('\n');
    const tools = contextNoteFacet({
      reason: 'project_scoped_rules',
      text: rule('/repo/lib/tools/AGENTS.md', '# Tools\nSource: quoted inside the rule body'),
    });
    expect(tools).toMatchObject({
      labelKey: 'systemNote.reasonProjectScopedRules', peek: '/repo/lib/tools/AGENTS.md',
      readableLines: 6,
    });
    expect(contextNoteFacet({
      reason: 'project_scoped_rules',
      text: `${rule('/repo/a/AGENTS.md', 'A')}\n\n${rule('/repo/b/AGENTS.md', 'B')}`,
    }).peek).toBe('/repo/a/AGENTS.md · /repo/b/AGENTS.md');
    expect(contextNoteFacet({
      reason: 'project_scoped_rules',
      text: 'Source: /wt/AGENTS.md — same bytes as /repo/AGENTS.md; that text applies within /wt too.',
    }).peek).toBe('/wt/AGENTS.md');
    expect(contextNoteFacet({ reason: 'project_scoped_rules', text: 'Superseded: combined snapshot.' }).peek)
      .toBe('Superseded: combined snapshot.');
    expect(contextNoteFacet({ reason: 'skills_index', text: rule('/repo/AGENTS.md', 'x') }).peek)
      .toBe('Project Intelligence — AGENTS.md');
  });

  it('keeps an unknown or inherited reason as its raw id', () => {
    for (const reason of ['brand_new_reason', '__proto__', 'constructor']) {
      const facet = contextNoteFacet({ reason, text: 'x' });
      expect(facet.labelKey).toBe('');
      expect(facet.fallbackLabel).toBe(reason);
      expect(facet.authority).toBe('');
      expect(hasIcon(facet.glyph)).toBe(true);
    }
  });

  it('names a reason-less witness by its kind', () => {
    expect(contextNoteFacet({ noteKind: 'session-context', text: '' }).labelKey)
      .toBe('systemNote.reasonSessionContext');
    expect(contextNoteFacet({ noteKind: 'context-injection', text: '' }).labelKey)
      .toBe('systemNote.contextInjectionLabel');
  });

  it('resolves every registry glyph in the shared icon set', () => {
    const missing = Object.entries(SYSTEM_NOTE_REASON_PRESENTATION)
      .filter(([, presentation]) => !hasIcon(presentation.glyph))
      .map(([reason, presentation]) => `${reason}:${presentation.glyph}`);
    expect(missing).toEqual([]);
  });
});
