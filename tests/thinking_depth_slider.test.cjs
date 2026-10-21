const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');
const { JSDOM } = require('jsdom');
const moduleUnderTest = { exports: {} };
new Function('module', 'exports', ts.transpileModule(fs.readFileSync('frontend/src/features/thinking-depth-slider.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText)(moduleUnderTest, moduleUnderTest.exports);
const { createThinkingDepthSlider, displayThinkingDepth, THINKING_DEPTH_LEVELS } = moduleUnderTest.exports;
const en = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/en.json', 'utf8'));
const zh = JSON.parse(fs.readFileSync('frontend/src/i18n/locales/zh.json', 'utf8'));

function setup(t) {
  const dom = new JSDOM(fs.readFileSync('index.html', 'utf8'));
  const document = dom.window.document;
  const commits = [];
  let copy = en;
  const controller = createThinkingDepthSlider({ document, translate: key => copy[key] || key, commit: depth => commits.push(depth) });
  const inputs = [...document.querySelectorAll('[data-thinking-depth-slider] input')];
  const event = (input, type, value, properties = {}) => {
    if (value !== undefined) input.value = String(value);
    input.dispatchEvent(Object.assign(new dom.window.Event(type, { bubbles: true }), {
      pointerId: 1, button: 0, isPrimary: true, ...properties,
    }));
  };
  t.after(() => { controller.destroy(); dom.window.close(); });
  return { controller, document, inputs, commits, event, language(next) {
    copy = next;
    dom.window.dispatchEvent(new dom.window.Event('tofu:language-change'));
  } };
}

test('six levels and native keyboard steps, effort value beside the heading, no scale clutter', t => {
  assert.deepEqual([...THINKING_DEPTH_LEVELS], ['off', 'low', 'medium', 'high', 'xhigh', 'max']);
  assert.equal(displayThinkingDepth(null), 'medium');
  assert.equal(displayThinkingDepth('ultra'), 'ultra');
  assert.equal(displayThinkingDepth('low'), 'low');
  const s = setup(t);
  s.controller.sync(null, true);
  assert.equal(s.inputs.length, 2);
  for (const input of s.inputs) {
    assert.equal(input.value, '2');
    assert.equal(input.step, '1');
    assert.ok(s.document.querySelector(`label[for="${input.id}"]`));
    assert.equal(input.getAttribute('aria-valuetext'), en['mobile.medium']);
    const card = input.closest('[data-thinking-depth-slider]');
    assert.equal(card.querySelectorAll('[data-depth]').length, 0);
    const value = card.querySelector('[data-depth-value]');
    assert.equal(value.textContent, en['mobile.medium']);
    assert.equal(value.getAttribute('aria-hidden'), 'true');
    assert.equal(card.querySelectorAll('output, p:not([hidden])').length, 0);
    assert.equal(input.hasAttribute('aria-describedby'), false);
  }
});

test('fractional mouse/touch preview does not snap, mutate the scale or commit before release', t => {
  const s = setup(t);
  s.controller.sync('medium', true);
  for (const input of s.inputs) {
    s.controller.sync('medium', true);
    s.commits.length = 0;
    const card = input.closest('[data-thinking-depth-slider]');
    const valueEl = card.querySelector('[data-depth-value]');
    s.event(input, 'pointerdown');
    for (const value of [2.1, 2.3, 2.75, 3.12, 3.46, 3.7]) {
      s.event(input, 'input', value);
      assert.ok(s.inputs.every(range => Number(range.value) === value && range.step === 'any'));
      assert.equal(Number(card.style.getPropertyValue('--depth-fraction')), value / 5);
      assert.deepEqual(s.commits, []);
    }
    // The heading value follows the preview in place; it is never rebuilt.
    assert.equal(card.querySelector('[data-depth-value]'), valueEl);
    assert.equal(valueEl.textContent, en['mobile.xhigh']);
    assert.equal(input.getAttribute('aria-valuetext'), en['mobile.xhigh']);
    // Pointerup outside the input still commits once; native change may follow.
    s.event(s.document.body, 'pointerup');
    s.event(input, 'change');
    assert.ok(s.inputs.every(range => range.value === '4' && range.step === '1'));
    assert.deepEqual(s.commits, ['xhigh']);
  }
});

test('round-trip within the same rung and a no-move click do not rewrite persisted settings', t => {
  const s = setup(t);
  s.controller.sync('medium', true);
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'input', 2.3);
  s.event(s.inputs[0], 'pointerup');
  s.event(s.inputs[0], 'change');
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'pointerup');
  assert.ok(s.inputs.every(input => input.value === '2' && input.step === '1'));
  assert.deepEqual(s.commits, []);
  // Without a pointer transaction the browser's native integer keyboard change works.
  s.event(s.inputs[1], 'input', 3);
  s.event(s.inputs[1], 'change');
  assert.deepEqual(s.commits, ['high']);
});

test('locale readiness/switch relabels without snapping the active drag or committing', t => {
  const s = setup(t);
  s.controller.sync('high', true);
  const input = s.inputs[0];
  s.event(input, 'pointerdown');
  s.event(input, 'input', 4.7);
  s.language(zh);
  assert.equal(input.value, '4.7');
  assert.equal(input.getAttribute('aria-valuetext'), zh['mobile.max']);
  assert.equal(input.closest('[data-thinking-depth-slider]').querySelector('[data-depth-value]').textContent, 'Max');
  assert.deepEqual(s.commits, []);
  s.event(input, 'pointercancel');
  assert.equal(input.value, '3');
  s.controller.destroy();
  s.language(en);
  assert.equal(input.getAttribute('aria-valuetext'), zh['mobile.high']);
  assert.equal(input.closest('[data-thinking-depth-slider]').querySelector('[data-depth-value]').textContent, 'High');
});

for (const cancellation of ['pointercancel', 'Escape', 'blur', 'sync', 'disabled']) {
  test(`${cancellation} discards the preview and suppresses late native commit`, t => {
    const s = setup(t);
    const input = s.inputs[0];
    s.controller.sync('medium', true);
    s.event(input, 'pointerdown');
    s.event(input, 'input', 4.7);
    if (cancellation === 'sync' || cancellation === 'disabled') s.controller.sync('low', cancellation !== 'disabled');
    else if (cancellation === 'Escape') s.event(input, 'keydown', undefined, { key: 'Escape' });
    else if (cancellation === 'blur') s.event(s.document.defaultView, 'blur');
    else s.event(input, cancellation);
    const expected = ['sync', 'disabled'].includes(cancellation) ? '1' : '2';
    s.event(input, 'input', 5);
    s.event(s.document.body, 'pointerup');
    s.event(input, 'change');
    assert.equal(input.value, expected);
    assert.equal(input.step, '1');
    assert.deepEqual(s.commits, []);
    s.controller.sync('medium', true);
    s.event(input, 'pointerdown');
    s.event(input, 'input', 4.7);
    s.event(input, 'pointerup');
    assert.deepEqual(s.commits, ['max']);
  });
}

for (const cancellation of ['pointercancel', 'blur', 'sync']) {
  test(`${cancellation} without pointerup permits a fresh keyboard gesture on either control`, t => {
    const s = setup(t);
    for (const keyboardInput of s.inputs) {
      s.controller.sync('medium', true);
      s.commits.length = 0;
      s.event(s.inputs[0], 'pointerdown');
      s.event(s.inputs[0], 'input', 4.7);
      if (cancellation === 'sync') s.controller.sync('low', true);
      else s.event(cancellation === 'blur' ? s.document.defaultView : s.inputs[0], cancellation);
      s.event(keyboardInput, 'keydown', undefined, { key: 'ArrowRight' });
      s.event(keyboardInput, 'input', 3);
      s.event(keyboardInput, 'change');
      assert.deepEqual(s.commits, ['high']);
      assert.ok(s.inputs.every(input => input.value === '3' && input.getAttribute('aria-valuetext') === en['mobile.high']));
    }
  });
}

test('only the owning pointer can cancel and change-before-pointerup commits once across synchronous sync', t => {
  const s = setup(t);
  const input = s.inputs[0];
  s.controller.sync('medium', true);
  s.event(input, 'pointerdown');
  s.event(input, 'input', 4.7);
  s.event(s.document.body, 'pointercancel', undefined, { pointerId: 2 });
  assert.equal(input.value, '4.7');
  s.event(input, 'change');
  // The production commit adapter synchronously projects committed config.
  s.controller.sync('max', true);
  s.event(input, 'pointerup');
  assert.deepEqual(s.commits, ['max']);
  assert.ok(s.inputs.every(range => range.value === '5' && range.step === '1'));
});

test('captured pointer coordinates preserve grab offset, clamp endpoints and read layout once', t => {
  const s = setup(t);
  const input = s.inputs[0];
  let reads = 0;
  let captured = null;
  input.getBoundingClientRect = () => { reads++; return { left: 100, width: 226 }; };
  input.setPointerCapture = id => { captured = id; };
  input.hasPointerCapture = id => captured === id;
  input.releasePointerCapture = () => { captured = null; };
  s.controller.sync('medium', true);
  // Current center=193; grab 5px to its right without jumping the ball.
  s.event(input, 'pointerdown', undefined, { clientX: 198 });
  assert.equal(input.value, '2');
  assert.equal(captured, 1);
  s.event(input, 'pointermove', undefined, { clientX: 200 });
  assert.equal(Number(input.value), 2.05);
  // A touch engine's following native input cannot replace the captured
  // pointer position with its separately rounded coordinate.
  s.event(input, 'input', 2.0498);
  assert.ok(s.inputs.every(range => Number(range.value) === 2.05));
  s.event(input, 'pointermove', undefined, { clientX: 300, pointerId: 2 });
  assert.equal(Number(input.value), 2.05);
  s.event(input, 'pointermove', undefined, { clientX: 999 });
  assert.equal(input.value, '5');
  s.event(input, 'pointermove', undefined, { clientX: 0 });
  assert.equal(input.value, '0');
  assert.equal(reads, 1);
  assert.deepEqual(s.commits, []);
  s.event(input, 'pointerup');
  assert.equal(captured, null);
  assert.deepEqual(s.commits, ['off']);
});

test('restore aliases, secondary pointers, model capability and teardown never introduce writes', t => {
  const s = setup(t);
  const input = s.inputs[0];
  s.controller.sync('ultra', true);
  assert.ok(s.inputs.every(range => range.value === '6'));
  s.event(input, 'change');
  s.event(input, 'pointerdown', undefined, { button: 2 });
  assert.equal(input.step, '1');
  s.controller.sync('off', true);
  assert.ok(s.inputs.every(range => range.value === '0'));
  s.controller.sync('high', false);
  assert.ok(s.inputs.every(range => range.disabled));
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, 'none');
  s.event(input, 'change', 5);
  assert.deepEqual(s.commits, []);
  s.controller.sync('medium', true);
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, '');
  s.event(input, 'pointerdown');
  s.event(input, 'input', 4.2);
  s.controller.destroy();
  assert.equal(input.step, '1');
  s.event(input, 'pointerup');
  s.event(input, 'change', 5);
  assert.deepEqual(s.commits, []);
});

const { projectReasoningControl } = moduleUnderTest.exports;
test('server projection keys creator/model/provider exactly and pending provider/offering', () => {
  const row = { creator_id: 'a', model_id: 'same', provider_id: '', offering_id: '', support: 'supported', can_disable: false, depths: ['default', 'off', 'lowest', 'high'] };
  const rows = [row, { ...row, creator_id: 'b', depths: ['max'] }, { ...row, provider_id: 'p', depths: ['low'] }, { ...row, provider_id: 'p', offering_id: 'o', depths: ['medium'] }];
  assert.deepEqual(projectReasoningControl(rows, { creator_id: 'a', model_id: 'same' }).levels, ['default', 'lowest', 'high']);
  assert.deepEqual(projectReasoningControl(rows, { creator_id: 'a', model_id: 'same' }, 'p').levels, ['default', 'low']);
  assert.deepEqual(projectReasoningControl(rows, { provider_id: 'p', offering_id: 'o' }).levels, ['default', 'medium']);
  assert.equal(projectReasoningControl(rows, { creator_id: 'missing', model_id: 'same' }).notice, 'mobile.depthUnconfigured');
  assert.equal(projectReasoningControl(undefined, null), undefined);
  /* A persisted provider pin that no longer exists (provider renamed/removed)
   * must not look "unconfigured": the model-level auto row is the fallback. */
  const kimi = { creator_id: 'moonshot', model_id: 'kimi-k3', provider_id: '', offering_id: '', support: 'supported', can_disable: false, depths: ['default', 'low', 'high', 'max'] };
  const kimiRows = [kimi,
    { ...kimi, provider_id: 'example-corp' }, { ...kimi, provider_id: 'example-corp', offering_id: 'off_a' },
    { ...kimi, provider_id: 'catpaw' }, { ...kimi, provider_id: 'catpaw', offering_id: 'off_b' }];
  assert.equal(projectReasoningControl(kimiRows, { creator_id: 'moonshot', model_id: 'kimi-k3' }, 'yourprovider').notice, '');
  assert.deepEqual(projectReasoningControl(kimiRows, { creator_id: 'moonshot', model_id: 'kimi-k3' }, 'yourprovider').levels, ['default', 'low', 'high', 'max']);
  for (const support of ['unknown', 'unsupported']) assert.deepEqual(projectReasoningControl([{ ...row, support }], { creator_id: 'a', model_id: 'same' }).levels, ['default']);
});
test('a displayed model with no confirmed ref resolves by unique model id (provisional default)', () => {
  /* Regression: a fresh conversation paints the server default model
   * (kimi-k3) with config.modelRef still null. The projection matched only
   * exact creator_id+model_id refs, so the depth slider read
   * 'Max — 不可用' / depthUnconfigured despite an installed route. The
   * displayed model id is the intent and resolves to its unique identity. */
  const kimi = { creator_id: 'moonshot', model_id: 'kimi-k3', provider_id: '', offering_id: '', support: 'supported', can_disable: false, default_depth: 'max', depths: ['default', 'low', 'high', 'max'] };
  const kimiRows = [kimi,
    { ...kimi, provider_id: 'example-corp' }, { ...kimi, provider_id: 'example-corp', offering_id: 'off_a' },
    { ...kimi, provider_id: 'catpaw', availability: 'no_route' }];
  // Before the fix: null ref → unconfigured notice even though kimi-k3 shows.
  const resolved = projectReasoningControl(kimiRows, null, '', 'kimi-k3');
  assert.equal(resolved.notice, '');
  assert.equal(resolved.visible, true);
  assert.equal(resolved.available, true);
  assert.equal(resolved.defaultDepth, 'max');
  assert.deepEqual(resolved.levels, ['default', 'low', 'high', 'max']);
  // A provider pin narrows the fallback to that provider's route state.
  const pinned = projectReasoningControl(kimiRows, null, 'catpaw', 'kimi-k3');
  assert.equal(pinned.notice, 'mobile.depthNoRoute');
  assert.equal(pinned.available, false);
  // An unknown displayed model stays hidden/unconfigured.
  const unknown = projectReasoningControl(kimiRows, null, '', 'no-such-model');
  assert.equal(unknown.notice, 'mobile.depthUnconfigured');
  assert.equal(unknown.visible, false);
  // An ambiguous model id (two creators) must not silently bind one.
  const a = { creator_id: 'a', model_id: 'dup', provider_id: '', offering_id: '', support: 'supported', can_disable: false, depths: ['high'] };
  const b = { creator_id: 'b', model_id: 'dup', provider_id: '', offering_id: '', support: 'supported', can_disable: false, depths: ['max'] };
  assert.equal(projectReasoningControl([a, b], null, '', 'dup').notice, 'mobile.depthUnconfigured');
  // A stale ref for a DIFFERENT model never resolves via the displayed id.
  assert.equal(projectReasoningControl(kimiRows, { creator_id: 'moonshot', model_id: 'kimi-k2' }, '', 'kimi-k3').notice, 'mobile.depthUnconfigured');
});
test('dynamic levels distinguish default and lowest and preserve unavailable stored intent', t => {
  const s = setup(t);
  s.controller.sync('default', { levels: ['default', 'lowest', 'medium'] });
  assert.ok(s.inputs.every(input => input.max === '1' && input.value === '0'));
  s.event(s.inputs[0], 'input', 0);
  s.event(s.inputs[0], 'change');
  assert.deepEqual(s.commits, ['lowest']);
  s.controller.sync('future-depth', { levels: ['default'], visible: false });
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, '');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /future-depth.*Unavailable/);
  s.event(s.inputs[0], 'change');
  assert.deepEqual(s.commits, ['lowest']);
  s.event(s.inputs[0], 'input', 0);
  s.event(s.inputs[0], 'change');
  assert.deepEqual(s.commits, ['lowest']);
  assert.equal(s.inputs[0].disabled, true);
  assert.equal(s.document.getElementById('mobileDepthSection').style.display, '');
});
test('identical capability refresh preserves a pointer gesture', t => {
  const s = setup(t);
  const options = { levels: ['default', 'low', 'medium', 'high', 'max'] };
  s.controller.sync('medium', options);
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'input', 2.6);
  s.controller.sync('medium', { levels: [...options.levels] });
  assert.equal(s.inputs[0].value, '2.6');
  s.event(s.inputs[0], 'pointerup');
  assert.deepEqual(s.commits, ['max']);
});
test('single-rung rebinding holds the verbatim stored intent and never commits from sync', t => {
  const s = setup(t);
  s.controller.sync('medium', { levels: ['default', 'medium', 'max'] });
  s.event(s.inputs[0], 'pointerdown');
  s.event(s.inputs[0], 'input', 2);
  // Rebinding to a ladder that cannot represent 'medium' must not rewrite
  // saved intent from the render path. sync() only paints; 'medium' stays the
  // verbatim committed depth and nothing commits from the render path.
  s.controller.sync('medium', { levels: ['default', 'low'], restoration: {fallback: 'default', replacements: {medium: 'low'}} });
  assert.deepEqual(s.commits, []);
  s.event(s.inputs[0], 'pointerup');
  s.event(s.inputs[0], 'change');
  // The stale drag was cancelled by the re-sync; it releases without persisting.
  assert.deepEqual(s.commits, []);
  // The server restoration table maps 'medium' onto 'low': the ladder is NOT
  // inflated with an unusable rung — the thumb parks on the replacement rung
  // and the label keeps the saved intent, flagged unavailable.
  assert.equal(s.inputs[0].max, '0');
  assert.equal(s.inputs[0].value, '0');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Unavailable/);
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Med/);
});

test('restoration parks the thumb on the mapped rung and preserves verbatim intent', t => {
  // The restart scenario: a depth saved for a wider ladder meets a narrower
  // route. The slider paints the executable replacement, keeps the verbatim
  // saved depth in storage, and binds back when the wide ladder returns.
  const s = setup(t);
  const narrow = { levels: ['default', 'low', 'high', 'max'], restoration: {fallback: 'default', replacements: {medium: 'low'}} };
  const wide = { levels: ['default', 'off', 'low', 'medium', 'high', 'xhigh', 'max'] };
  s.controller.sync('medium', narrow);
  assert.deepEqual(s.commits, []);
  assert.equal(s.inputs[0].max, '2');
  // The thumb parks on the mapped replacement rung 'low' (native index 0),
  // flagged with the saved intent.
  assert.equal(s.inputs[0].value, '0');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Unavailable/);
  // No synthesized unusable rung: the slider never grew a 'medium' position.
  const card = s.inputs[0].closest('[data-thinking-depth-slider]');
  assert.equal(card.querySelector('.depth-slider-dots').children.length, 3);
  // The parked thumb is on a selectable rung; an explicit gesture still
  // commits the rung it lands on, persistence stays gesture-only.
  s.event(s.inputs[0], 'input', 1);
  s.event(s.inputs[0], 'change');
  assert.deepEqual(s.commits, ['high']);
  // Rebinding to a ladder that can express the saved depth restores it as-is.
  s.controller.sync('medium', wide);
  assert.equal(s.inputs[0].value, '2');
  assert.equal(s.inputs[0].getAttribute('aria-valuetext'), en['mobile.medium']);
  assert.deepEqual(s.commits, ['high']);
  // A depth the restoration table cannot map (unknown future token, no
  // 'default' rung in the native ladder) is still appended as an unavailable
  // flagged rung: verbatim intent survives, never silently rebound.
  s.controller.sync('unknown-future-token', { levels: ['default', 'low', 'high'], restoration: {fallback: 'default', replacements: {medium: 'low'}} });
  assert.equal(s.inputs[0].max, '2');
  assert.equal(s.inputs[0].value, '2');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Unavailable/);
  assert.deepEqual(s.commits, ['high']);
});

test('opaque native names preserve declared order without invented effort rankings', t => {
  const s = setup(t);
  const options = { levels: ['default', 'careful', 'deepest'], defaultDepth: 'careful', restoration: {fallback: 'default', replacements: {off: 'careful'}} };
  // The render path paints the mapped replacement for a depth the ladder
  // cannot take, but commits nothing: persistence is gesture-only.
  s.controller.sync('off', options);
  assert.deepEqual(s.commits, []);
  // 'off' maps onto 'careful': the thumb parks on the replacement rung
  // (native index 0) and the label keeps the saved intent, flagged.
  assert.equal(s.inputs[0].value, '0');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Unavailable/);
  s.controller.sync('obsolete-custom-token', options);
  assert.deepEqual(s.commits, []);
  // An unmapped depth ('obsolete-custom-token', fallback 'default' not in
  // the native ladder) is still appended as an unavailable flagged rung;
  // the label keeps the saved intent verbatim.
  assert.equal(s.inputs[0].max, '2');
  assert.equal(s.inputs[0].value, '2');
  assert.match(s.inputs[0].getAttribute('aria-valuetext'), /Unavailable/);
});


test('native three levels, independent default, dynamic dots and genuine ultra', t => {
  const s = setup(t);
  s.controller.sync('default', { levels: ['default', 'low', 'high', 'max'], defaultDepth: 'max' });
  assert.equal(s.inputs[0].max, '2');
  assert.equal(s.inputs[0].value, '2');
  const card = s.inputs[0].closest('[data-thinking-depth-slider]');
  assert.equal(card.querySelector('.depth-slider-dots').children.length, 3);
  assert.equal(card.querySelector('[data-depth-default]').getAttribute('aria-pressed'), 'true');
  s.event(s.inputs[0], 'input', 1);
  s.event(s.inputs[0], 'change');
  assert.deepEqual(s.commits, ['high']);
  card.querySelector('[data-depth-default]').click();
  assert.deepEqual(s.commits, ['high', 'default']);
  s.controller.sync('ultra', { levels: ['default', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'] });
  assert.equal(s.inputs[0].max, '5');
  assert.equal(s.inputs[0].value, '5');
  assert.equal(card.querySelector('[data-depth-value]').textContent, 'Ultra');
  assert.equal(card.querySelector('.depth-slider-dots').children.length, 6);
});

test('unroutable Opus keeps its native ladder visible, disabled and explained', t => {
  const s = setup(t);
  const row = {creator_id:'anthropic',model_id:'claude-opus-5',provider_id:'',offering_id:'',
    support:'supported',availability:'no_route',can_disable:true,
    default_depth:'high',depths:['default','off','low','medium','high','xhigh','max']};
  s.controller.sync('max', projectReasoningControl([row], row));
  assert.equal(s.inputs[0].max, '5');
  assert.equal(s.inputs[0].disabled, true);
  assert.equal(s.document.getElementById('thinkingDepthSection').style.display, '');
  const note = s.document.querySelector('[data-depth-notice]');
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, en['mobile.depthNoRoute']);
  s.event(s.inputs[0], 'change', 1);
  assert.deepEqual(s.commits, []);
});
