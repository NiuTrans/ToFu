/** Structured multimedia receipt outcomes, escaping and WorkUnit demand boundaries. */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');
const { JSDOM } = require('jsdom');
const root = path.resolve(__dirname, '..');
const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'tofu-motion-card-'));
const bundle = path.join(temporary, 'card.cjs');
execFileSync(process.execPath, [path.join(root, 'scripts/vite_test_bundle.mjs'), '--bundle', '--format=cjs',
  '--platform=node', '--loader=ts', '--sourcefile=motion-tests.ts', `--outfile=${bundle}`], {
  cwd: root, input: `export { renderMotionCard } from ${JSON.stringify(path.join(root, 'frontend/src/conversation/ui/motion-card/index.ts'))};`,
  stdio: ['pipe', 'pipe', 'pipe'],
});
const { renderMotionCard } = require(bundle);
test.after(() => fs.rmSync(temporary, { recursive: true, force: true }));
function render(toolName, payload, extra = {}) {
  return renderMotionCard({ toolName, query: 'Handler call', roundNum: 7, toolContent: JSON.stringify(payload), ...extra },
    '<svg></svg>', '<span class="badge">accepted</span>', { text: (_, fallback) => fallback });
}
const text = html => {
  const dom = new JSDOM(html), result = dom.window.document.body.textContent;
  dom.window.close(); return result;
};

test('every supported tool has the correct human verb and source, including non-video production jobs', () => {
  const tools = ['motion_video_env_check', 'motion_video_storyboard_check', 'motion_video_check', 'motion_video_render',
    'motion_video_probe', 'motion_video_concat', 'motion_video_narrate', 'motion_video_mux', 'produce_video',
    'produce_report', 'produce_research', 'produce_slides', 'edit_slides', 'edit_video', 'patch_slides',
    'restyle_slides', 'revert_slides', 'revert_video'];
  for (const tool of tools) {
    const dom = new JSDOM(render(tool, { ok: true, output: 'artifact.mp4', changes: [], probe: { width: 1920, height: 1080 } }));
    const header = dom.window.document.querySelector('.ptool-text').textContent;
    assert.notEqual(header, tool); assert.notEqual(header, 'Handler call');
    assert.ok(dom.window.document.querySelector('.ptool-convmeta-why').textContent);
    const source = dom.window.document.querySelector('.ptool-convmeta-src').textContent;
    assert.equal(source, tool === 'produce_report' ? 'Report' : tool === 'produce_research' ? 'Research' : tool.includes('slides') ? 'Slides' : 'Video');
    dom.window.close();
  }
});

test('validation separates dependencies, gate errors, warnings, fixes and storyboard span', () => {
  const dependencies = text(render('motion_video_env_check', { ok: false, node: '/bin/node', chrome: null, issues: ['Missing chrome'] }));
  assert.match(dependencies, /missing dependencies/); assert.match(dependencies, /Missing chrome/); assert.match(dependencies, /not found/);
  const gates = text(render('motion_video_check', { ok: false, category: 'layout', elapsed: 0, gates: { lint: true, layout: false },
    errors: ['Overlap'], warnings: ['Small type'], fix_hints: ['Move title'] }));
  for (const value of ['gate failed', 'layout', '0s', 'Overlap', 'Small type', 'Move title']) assert.ok(gates.includes(value), value);
  assert.match(text(render('motion_video_storyboard_check', { span_s: [0, 15], errors: ['Gap'] })), /0s → 15s/);
});

test('media receipts preserve numeric zero, audio absence, duration and narration alignment/degradation', () => {
  const probe = text(render('motion_video_probe', { probe: { codec: 'h264', width: 1920, height: 1080, fps: 0, duration: 0, has_audio: false } }));
  for (const value of ['h264', '1920×1080', '0 fps', '0.00s', 'silent']) assert.ok(probe.includes(value), value);
  const narration = render('motion_video_narrate', { alignment: 'timeline.json', scenes: [{ scene_id: 'one', audio_duration: 2,
    target_duration: 3, srt_duration: 2, overflow: 1 }], overflow_total: 1 });
  assert.match(narration, /ptool-motion-scene-grew/); assert.match(text(narration), /audio 2.00s → scene 3.00s/);
  assert.match(text(render('motion_video_narrate', { degraded: true, detail: 'slot unavailable' })), /No TTS slot configured/);
  assert.match(text(render('motion_video_mux', { ok: true, output: 'final.mp4', duration: 0 })), /verified present/);
  assert.match(text(render('motion_video_render', { ok: false, category: 'timeout', detail: 'Killed render' })), /Killed render/);
});

test('slide patch and restyle rollback errors remain visible, while patch previews retain their existing bound and remainder', () => {
  const failed = text(render('patch_slides', { ok: false, errors: ['Invalid target', 'Layout regression'] }));
  assert.match(failed, /Invalid target/); assert.match(failed, /Layout regression/);
  const failedTheme = text(render('restyle_slides', { ok: false, errors: ['Unknown theme'], available_themes: ['calm', 'report'] }));
  assert.match(failedTheme, /Unknown theme/); assert.match(failedTheme, /calm, report/);
  const changes = Array.from({ length: 20 }, (_, n) => ({ op: 'set_text', target: `title-${n}`, before: 'before', after: 'after' }));
  const preview = render('patch_slides', { ok: true, changes });
  assert.match(preview, /title-11/); assert.doesNotMatch(preview, /title-12/); assert.match(preview, /\+8 more/);
  assert.match(text(render('revert_video', { ok: true, version: 0, scene_id: 'one', note: 'Snapshot restored' })), /v0/);
});

test('handler call and snippet fallback stay escaped, malformed JSON degrades without a parse exception', () => {
  const html = render('produce_slides', { ok: true, topic: '<script>bad()</script>', quality_hint: '<img onerror=bad()>' },
    { query: '<b>raw call</b>' });
  assert.doesNotMatch(html, /<script>|<img onerror=|<b>raw call/);
  assert.match(text(html), /<script>bad\(\)<\/script>/);
  const fallback = render('unknown-tool', {}, { toolContent: '{bad json', results: [{ snippet: '<b>fallback</b>' }] });
  assert.match(text(fallback), /<b>fallback<\/b>/);
  assert.equal(render('unknown-tool', {}, { query: '', results: [] }), '');
  assert.doesNotThrow(() => render('produce_video', {}, { toolContent: 'null' }));
  const narration = render('motion_video_narrate', { scenes: [{ scene_id: 'one', overflow: '<img src=x onerror=bad()>' }] });
  assert.doesNotMatch(narration, /<img src=x/);
  assert.match(text(narration), /<img src=x onerror=bad\(\)>/);
  const attribute = new JSDOM(render('produce_slides', { ok: true }, { roundNum: '7" onclick="bad()' }));
  assert.equal(attribute.window.document.querySelector('details').hasAttribute('onclick'), false);
  assert.equal(attribute.window.document.querySelector('details').dataset.rn, '7" onclick="bad()');
  attribute.window.close();
  const translated = renderMotionCard({ toolName: 'motion_video_narrate', roundNum: 1,
    toolContent: '{"scenes":[{"scene_id":"one"}]}' }, '', '', {
      text: (key, fallback) => key === 'motionRow.sceneDur' ? '<b>Literal caption</b>' : fallback,
    });
  assert.doesNotMatch(translated, /<b>Literal caption/);
  assert.match(text(translated), /<b>Literal caption<\/b>/);
});

test('WorkUnit demand is emitted only for a durable handle, never from a guessed payload task id', () => {
  let scans = 0;
  const ports = { text: (_, fallback) => fallback, scanWorkUnits: () => scans++ };
  const round = { toolName: 'produce_slides', roundNum: 5, toolContent: '{"ok":true,"task_id":"payload-only"}',
    results: [{ workUnitRef: { taskId: 'owner<&"', kind: 'slides-deck' } }] };
  const dom = new JSDOM(renderMotionCard(round, '', '', ports));
  assert.equal(dom.window.document.querySelector('[data-wu-task]').dataset.wuTask, 'owner<&"');
  assert.equal(scans, 1); dom.window.close();
  const html = renderMotionCard({ ...round, results: [] }, '', '', ports);
  assert.doesNotMatch(html, /data-wu-task|ptool-wu-live/); assert.equal(scans, 1);
});
