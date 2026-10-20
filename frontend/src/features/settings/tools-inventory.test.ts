import { describe, expect, it, vi } from 'vitest';
import enSource from '../../i18n/generated/en.generated.json?raw';
import zhSource from '../../i18n/generated/zh.generated.json?raw';

const runtime = vi.hoisted(() => ({ t: vi.fn() }));
vi.mock('../../feature-registry', () => ({ featureRegistry: runtime }));

import { familyVisible, renderFamily, renderToolRow, toolMatches } from './tools-inventory';

describe('tool inventory delivery translations', () => {
  it.each([
    ['en', enSource, 'retained'],
    ['zh', zhSource, '工具结果'],
  ])('renders and searches the shipped %s descriptions', (_language, source, query) => {
    const messages = JSON.parse(source) as Record<string, string>;
    runtime.t.mockImplementation((key: string) => messages[key] ?? key);
    const family = {
      key: 'tool_result_artifacts',
      source: 'builtin',
      tools: [{ name: 'read_tool_artifact' }],
    };
    const toolDescription = messages['toolsInv.tool.read_tool_artifact'];
    const familyDescription = messages['toolsInv.family.tool_result_artifacts'];
    expect(toolDescription).toBeTypeOf('string');
    expect(familyDescription).toBeTypeOf('string');
    expect(toolMatches(family.tools[0], query)).toBe(true);
    expect(familyVisible(family, query)).toBe(true);
    const markup = renderFamily(family, query);
    expect(markup).toContain(toolDescription);
    expect(markup).toContain(familyDescription);
  });

  it('preserves and escapes an external tool description without a catalog key', () => {
    runtime.t.mockImplementation((key: string) => key);
    const markup = renderToolRow({
      name: 'plugin__custom', description: 'Inspect <external> & retained data.',
    }, false);
    expect(markup).toContain('Inspect &lt;external&gt; &amp; retained data.');
  });
});
