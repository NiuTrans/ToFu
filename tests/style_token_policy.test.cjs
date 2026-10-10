/** Closed byte-copy exemptions cannot admit new raw styling. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');
const load = () => import('../scripts/style_token_policy.mjs');
const old = '.terminal {\n  color: #fbbf24;\n  padding: 5px;\n}\n';
const digest = createHash('sha256').update(old).digest('hex');

test('only identical legacy source can keep its old declarations', async () => {
  const policy = await load();
  assert.equal(policy.unchangedLegacyTokenCopy(old, digest), true);
  assert.equal(policy.unchangedLegacyTokenCopy(old.replace('#fbbf24', '#ffffff'), digest), false);
  assert.ok(policy.styleTokenViolations(old.replace('#fbbf24', '#ffffff'), 'owner.css').length);
});
test('edited owners may graduate through strict tokens without reapproving their hash', async () => {
  const policy = await load();
  const migrated = '.terminal {\n  color: var(--warning);\n  padding: var(--space-1);\n}\n';
  assert.equal(policy.unchangedLegacyTokenCopy(migrated, digest), false);
  assert.deepEqual(policy.styleTokenViolations(migrated, 'owner.css'), []);
});
test('another filename or added raw rule cannot inherit a blanket legacy exemption', async () => {
  const policy = await load();
  assert.equal(policy.unchangedLegacyTokenCopy(old, undefined), false);
  assert.equal(policy.unchangedLegacyTokenCopy(old + '.new {color:#fff}', digest), false);
  assert.ok(policy.styleTokenViolations(old, 'new.css').length);
});
test('malformed admission hashes fail rather than skip the strict gate', async () => {
  const policy = await load();
  for (const hash of ['', digest.toUpperCase(), digest + '0', true, 3, null]) {
    assert.throws(() => policy.unchangedLegacyTokenCopy(old, hash), /legacyTokenSha256/);
  }
});
test('comment edits do not turn frozen source into an extensible exemption', async () => {
  const policy = await load();
  const changed = '/* new comment */\n' + old;
  assert.equal(policy.unchangedLegacyTokenCopy(changed, digest), false);
  assert.ok(policy.styleTokenViolations(changed, 'owner.css').length);
});
test('existing paint, spacing and theme contracts remain enforced', async () => {
  const { styleTokenViolations } = await load();
  const css = '/* #fff rgba(1,2,3,1) */\n.x {\n color:rgba(1,2,3,0.2);\n gap: 3px;\n}\n[data-theme=dark] .x {color:var(--text)}';
  const violations = styleTokenViolations(css, 'owner.css', { themeAgnostic: true });
  assert.equal(violations.length, 3);
  assert.ok(violations.some(v => v.includes('functional color')));
  assert.ok(violations.some(v => v.includes('raw px')));
  assert.ok(violations.some(v => v.includes('[data-theme]')));
  assert.equal(styleTokenViolations('.x {color:#fff}', 'tokens.css', { allowColor: true }).length, 0);
});

test('minified, multiline and logical layout declarations cannot bypass tokens', async () => {
  const { styleTokenViolations } = await load();
  const css = '.x{padding:4px;margin-inline-start:-2px;inset-block:0.5px;}\n.y{gap:\n6px}';
  assert.equal(styleTokenViolations(css, 'compact.css').length, 4);
  assert.deepEqual(styleTokenViolations('#fff {padding:0; margin:var(--space-2)}', 'id.css'), []);
});

test('invalid CSS fails admission instead of hiding declarations from the scanner', async () => {
  const { styleTokenViolations } = await load();
  assert.throws(() => styleTokenViolations('.x { color:var(--text);', 'broken.css'), /Unclosed block/);
});
