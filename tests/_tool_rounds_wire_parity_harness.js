/* Wire-parity harness for _renderUnifiedToolLine (permanent regression gate).
 *
 * Evals a tool_rounds.js build with a minimal global surface, renders the
 * rounds battery (tests/_tool_rounds_wire_parity_rounds.json) through
 * _renderUnifiedToolLine, and prints JSON [{i, name, html, err}] on stdout.
 *
 * Usage: node tests/_tool_rounds_wire_parity_harness.js <tool_rounds.js> <rounds.json>
 *
 * The pytest wrapper (test_frontend_tool_rounds_wire_parity.py) compares
 * this output byte-for-byte against tests/_tool_rounds_wire_parity_baseline.json.
 * Any behavioural drift in the dispatcher or its 16 branch helpers flips
 * the gate red. NOTE: this script must call process.exit() — a JSDOM-style
 * harness without an explicit exit hangs past the 60s pytest timeout on
 * FUSE (pre-existing failure mode of the older approval-card harnesses).
 */
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const rounds = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// ── Minimal global surface tool_rounds.js touches at render time ──
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (k, d) => (d || k);
global.Icon = (n, s) => `<ICON:${n}:${s || ''}>`;
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = { location: { href: 'http://localhost/' }, addEventListener() {}, removeEventListener() {} };
global.document = {
  addEventListener() {}, removeEventListener() {},
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
};

eval(src);

const out = rounds.map((r, i) => {
  const { _isSearching, ...round } = r;
  let html, err = null;
  try { html = _renderUnifiedToolLine(round, !!_isSearching); }
  catch (e) { err = String(e && e.stack || e); }
  return { i, name: r._name || String(i), html, err };
});
process.stdout.write(JSON.stringify(out, null, 1));
process.exit(0);
