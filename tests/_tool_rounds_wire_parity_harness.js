/* Wire-parity harness for _renderUnifiedToolLine (permanent regression gate).
 *
 * Evals a tool_rounds.js build with a minimal global surface, renders the
 * rounds battery (tests/_tool_rounds_wire_parity_rounds.json) through
 * _renderUnifiedToolLine, and prints JSON [{i, name, html, err}] on stdout.
 *
 * Usage: node tests/_tool_rounds_wire_parity_harness.js <tool_rounds.js> <rounds.json> [tool_rounds_rich.js]
 *
 * The optional third argument evals the DEFERRED rich-render module
 * (ui/tool_rounds_rich.js, Epic-E sub-4) after the core file — the gate
 * runs in RICH mode (core + deferred landed), byte-identical to the
 * pre-split monolith for every branch.
 *
 * The pytest wrapper (test_frontend_tool_rounds_wire_parity.py) compares
 * this output byte-for-byte against tests/_tool_rounds_wire_parity_baseline.json.
 * Any behavioural drift in the dispatcher or its composed typed/lazy
 * presenters flips the gate red. NOTE: this script must call process.exit()
 * only after stdout drains — a JSDOM-style harness without an explicit exit
 * hangs past the 60s pytest timeout on FUSE, while an immediate exit truncates
 * snapshots once they exceed the pipe buffer.
 */
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');
const rounds = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));

// ── Minimal global surface tool_rounds.js touches at render time ──
global.escapeHtml = (s) => String(s == null ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
global.t = (key, paramsOrFallback) => {
  if (typeof paramsOrFallback === 'string') return paramsOrFallback;
  const messages = {
    'tool.resultStats': '{lines} 行 · {chars} 字符',
    'tool.resultTruncated': '结果过长，仅显示前 {n} 字符',
    'swarmCard.received': 'Received',
    'swarmCard.updateOne': 'sub-agent update',
    'swarmCard.updateMany': 'sub-agent updates',
    'swarmCard.remaining': '{r} running · {p} pending',
    'swarmCard.noPayload': 'No payload available.',
    'peerCard.noPayload': 'No message available.',
    'toolInjection.itemsLimit': 'Showing first {shown} of {total} injected items.',
    'toolInjection.contentLimit': 'Content truncated to the first {n} characters.',
    'peer.injectRowLabel': 'Received',
    'peer.injectRowOne': 'peer message',
    'peer.injectRowMany': 'peer messages',
    'peer.injectRowBadge': 'injected → context',
    'peer.jumpToConv': 'Click to open the conversation',
    'steer.injectRowLabel': 'You steered mid-turn',
    'steer.injectRowOne': 'steer message',
    'steer.injectRowMany': 'steer messages',
    'steer.noPayload': 'No message available.',
    'stall.injectRowLabel': 'Nudged the model to continue',
    'stall.reasonWithTool': '`{tool}` did not run, and the next round was text only — the model said what it would do, then stopped.',
    'stall.reasonGeneric': 'The previous tool call did not run, and the next round was text only.',
    'stall.bound': 'At most once per turn — if the model stalls again it is allowed to stop.',
    'stall.promptLabel': 'Sent to the model',
    'toolSearch.found': '{total} candidate matches · showing {shown}',
    'toolSearch.none': 'No matching tools',
    'toolSearch.more': 'more candidates available',
    'toolSearch.failOpen': 'full catalog restored',
    'inspect.opsTitle': 'Applied transform',
    'inspect.cropped': 'cropped',
    'inspect.gridOverlay': 'grid overlay',
    'inspect.rotated': 'rotated {deg}',
    'inspect.zoom': 'zoom {factor}',
    'inspect.fitTo': 'fit to {size}',
    'inspect.fullFrame': 'full frame',
    'inspect.opsSep': ', ',
    'toolImage.images': '{n} images',
    'toolImage.limit': 'Showing first {shown} of {total} images.',
    'toolImage.edited': 'Edited',
    'toolImage.editedTitle': 'Edited an existing image',
    'toolImage.generated': 'Generated',
    'toolImage.generatedTitle': 'Generated from a text prompt',
    'toolImage.svgVersion': 'SVG version generated',
    'toolImage.openSvg': 'Open SVG',
    'toolImage.savedProject': 'Saved to project: {path}',
    'toolImage.svgSavedProject': 'SVG saved to project: {path}',
    'toolImage.sourceAlt': 'source image',
    'toolImage.before': 'Before',
    'toolImage.after': 'After',
    'toolImage.downloadPng': 'Download PNG',
    'toolImage.fullscreen': 'Fullscreen',
    'toolImage.failed': 'failed',
    'toolImage.editFailed': 'Image editing failed',
    'toolImage.generateFailed': 'Image generation failed',
    'toolImage.editing': 'editing…',
    'toolImage.generating': 'generating…',
    'toolImage.done': 'done',
    'toolCmd.showResult': 'Show result',
    'toolBrowserExecution.executeJs': 'Execute JS',
    'toolBrowserExecution.ok': 'ok',
    'toolBrowserExecution.error': 'error',
    'toolBrowserExecution.argumentsLimit': 'Arguments exceed the {n}-character display budget.',
    'toolBrowserExecution.codeLimit': 'Code truncated to the first {n} characters.',
    'toolBrowserExecution.descriptionLimit': 'Description truncated to the first {n} characters.',
    'toolBrowserExecution.resultLimit': 'Result truncated to the first {n} characters.',
    'toolCommandExecution.running': 'Running...',
    'toolCommandExecution.timeout': 'timeout',
    'toolCommandExecution.notRun': 'not run',
    'toolCommandExecution.exitCode': 'exit {code}',
    'toolCommandExecution.liveOutputElided': '… [{n} earlier chars elided] …\n',
    'toolCommandExecution.argumentsLimit': 'Arguments exceed the {n}-character display budget.',
    'toolCommandExecution.commandLimit': 'Command truncated to the first {n} characters.',
    'toolCommandExecution.descriptionLimit': 'Description truncated to the first {n} characters.',
    'toolCommandExecution.outputLimit': 'Output truncated to the first {n} characters.',
    'toolCommandExecution.qrLimit': 'Showing first {shown} of {total} QR images.',
    'toolApproval.awaiting': 'awaiting approval',
    'toolApproval.approve': 'Approve',
    'toolApproval.reject': 'Reject',
    'toolApproval.oneEditAcross': '{count} edit across {path}',
    'toolApproval.manyEditsAcross': '{count} edits across {path}',
    'toolApproval.moreLines': '… {count} more lines',
    'toolApproval.editFallback': 'Edit {index}',
    'toolApproval.editStats': '{searchLines}→{replaceLines} lines',
    'toolApproval.moreLinesWithTotals': '… {count} more lines ({totalLines} lines · {totalChars} chars total)',
    'toolApproval.moreEdits': '… and {count} more edits',
    'toolApproval.moreLinesUnknown': '… more lines',
    'toolApproval.writeMeta': '{lines} lines · {chars} chars',
    'toolApproval.previewLimit': 'Preview truncated to the first {n} characters.',
    'toolApproval.riskFieldsLimit': 'Showing first {shown} of {total} risk fields.',
    'toolApproval.approvalIdLimit': 'Approval unavailable: identifier exceeds {n} characters.',
    'toolCmd.finished': 'finished',
    'toolCmd.grepSearchIntercepted': 'grep_search takeover',
    'toolCmd.interruptedBadge': '⏸ interrupted',
    'project.qrScan': 'Scannable QR code',
    'project.qrScanMulti': 'scannable QR codes',
  };
  let value = messages[key] || key;
  if (!paramsOrFallback || typeof paramsOrFallback !== 'object') return value;
  return value.replace(/\{([A-Za-z0-9_]+)\}/g, (token, name) => (
    Object.prototype.hasOwnProperty.call(paramsOrFallback, name)
      ? String(paramsOrFallback[name]) : token
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

eval(src);
if (process.argv[4]) {
  eval(fs.readFileSync(process.argv[4], 'utf8'));
}

const out = rounds.map((r, i) => {
  const { _isSearching, ...round } = r;
  let html, err = null;
  try { html = _renderUnifiedToolLine(round, !!_isSearching); }
  catch (e) { err = String(e && e.stack || e); }
  return { i, name: r._name || String(i), html, err };
});
process.stdout.write(JSON.stringify(out, null, 1), () => process.exit(0));
