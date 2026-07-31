
const { setup } = require(process.env.JSDOM_HARNESS);

const conv = { id: 'c1', messages: [
  { _msgId: 'm1', role: 'user', content: 'u1' },
  { _msgId: 'm2', role: 'assistant', content: 'a1' },
]};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner">' +
        '<div class="message" id="msg-0" data-msg-id="m1">u1-static</div>' +
        '<div class="message" id="msg-1" data-msg-id="m2">a1-static</div>' +
        '</div></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    renderMessage: (msg, idx) =>
      '<div class="message" id="msg-' + idx + '" data-msg-id="' + msg._msgId +
      '">' + msg.content + '-R</div>',
    _ensureMsgId: () => {},
    _convRenderFingerprint: () => 'fp',
    _lastRenderedFingerprint: '',
  },
});

/* Legacy semantics 1: no existing node + no opts.append → refuse (false). */
const ghost = { _msgId: 'm9', role: 'user', content: 'ghost' };
conv.messages.push(ghost);
const r1 = window.ConvView.upsertMessage('c1', ghost);
check('alias_no_existing_no_append_refuses', r1 === false);
check('alias_refusal_did_not_append', !document.getElementById('msg-2'));
conv.messages.pop();

/* Legacy semantics 2: existing node → replace in place. */
const r2 = window.ConvView.upsertMessage('c1', conv.messages[0]);
check('alias_existing_replaced', r2 === true &&
  document.getElementById('msg-0').textContent === 'u1-R');

/* ① sweep on ALL paths: plant a drifted twin for m2, upsert → twin evicted. */
const inner = document.getElementById('chatInner');
inner.insertAdjacentHTML('beforeend',
  '<div class="message" id="msg-7" data-msg-id="m2">TWIN</div>');
check('twin_planted', inner.querySelectorAll('[data-msg-id="m2"]').length === 2);
const r3 = window.ConvView.upsertMessage('c1', conv.messages[1]);
check('alias_sweep_evicts_twin', r3 === true &&
  inner.querySelectorAll('[data-msg-id="m2"]').length === 1 &&
  document.getElementById('msg-1').textContent === 'a1-R');

/* Legacy semantics 3: opts.append=true with no existing → appends. */
const tail = { _msgId: 'm3', role: 'assistant', content: 'a2' };
conv.messages.push(tail);
const r4 = window.ConvView.upsertMessage('c1', tail, { append: true });
check('alias_append_true_appends', r4 === true &&
  !!document.querySelector('[data-msg-id="m3"]'));

report();
