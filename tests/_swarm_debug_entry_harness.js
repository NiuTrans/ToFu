/* Harness for _renderDebugEntry (swarm inspector-stream regression gate).
 *
 * Evals the materialized tool_rounds.js with debug_mode ON and renders the
 * debug anchor for two round shapes:
 *   - swarm: carries agentId + a 1-based llmRound → the anchor must target
 *     the agent's OWN Request Inspector stream `{parent}#agent:{agentId}`
 *     and use the llmRound AS the snapshot roundNum;
 *   - chat: no agentId → the legacy 0-based llmRound + 1 mapping.
 * Prints JSON [{name, html}] on stdout.
 *
 * Usage: node tests/_swarm_debug_entry_harness.js <tool_rounds.js>
 */
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (key, params) => {
  if (!params || typeof params !== 'object') return key;
  return key.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name]) : token
  ));
};
global.Icon = (n, s) => `<ICON:${n}:${s || ''}>`;
global.renderMarkdown = (s) => s;
global._shortUrl = (u) => u;
global.formatNumber = (n) => String(n);
global.window = { location: { href: 'http://localhost/' }, addEventListener() {}, removeEventListener() {} };
global.document = {
  addEventListener() {}, removeEventListener() {},
  createElement: () => ({ style: {}, setAttribute() {}, appendChild() {} }),
};
global._featureFlags = { debug_mode: true };

eval(src);

const cases = [
  {
    name: 'swarm',
    r: {
      roundNum: 3, llmRound: 3, agentId: 'agent-9',
      toolCallId: 'flow-tool-x', toolName: 'run_command',
      _taskId: 'task-1',
    },
  },
  {
    name: 'chat',
    r: {
      roundNum: 3, llmRound: 2,
      toolCallId: 'call-1', toolName: 'read_files',
      _taskId: 'task-1',
    },
  },
];
const out = cases.map((c) => ({ name: c.name, html: _renderDebugEntry(c.r) }));
process.stdout.write(JSON.stringify(out), () => process.exit(0));
