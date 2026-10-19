import { describe, expect, it } from 'vitest';
import { toolFailureText } from './tool-failure-presentation';
const fallback = 'The tool failed — details are in the output.';
describe('safe tool failure diagnostics', () => {
  it.each([
    { error: 'experiment has not run yet' },
    { error_detail: { message: 'experiment has not run yet' } },
    { summary: 'MCP Error: {"ok":false,"error":"experiment has not run yet","nextCursor":"trq1.private"}' },
    { result: { summary: JSON.stringify({ error: { message: 'experiment has not run yet' } }) } },
  ])('unwraps known bounded failure fields', value => {
    expect(toolFailureText(JSON.stringify(value), fallback)).toBe('experiment has not run yet');
  });
  it('retains safe structured codes and clips within 512 units', () => {
    expect(toolFailureText({ error: { code: 'not_found', message: 'x'.repeat(2000) } }, fallback)).toHaveLength(512);
    expect(toolFailureText({ error: { code: 'not_found', message: 'missing' } }, fallback)).toBe('not_found: missing');
  });
  it.each(['{broken', 'MCP Error: {broken', '{"error":"' + 'x'.repeat(40000) + '"}',
    'Error artifactRef=tool-result:private', 'Error trq1.signed.private',
    'Error ' + 'x'.repeat(600) + ' nextCursor=private', 'Error ' + 'a'.repeat(64),
  ])('keeps unsafe or unparseable output out of summaries', value => {
    expect(toolFailureText(value, fallback)).toBe(fallback);
  });
  it('bounds cyclic and overdeep wrappers without mutation', () => {
    const cyclic: Record<string, unknown> = {}; cyclic.result = cyclic;
    expect(toolFailureText(cyclic, fallback)).toBe(fallback);
    let deep: unknown = { error: 'secret depth' };
    for (let i = 0; i < 10; i++) deep = { result: deep };
    expect(toolFailureText(deep, fallback)).toBe(fallback);
  });
});
