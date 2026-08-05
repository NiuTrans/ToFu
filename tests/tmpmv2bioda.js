
const { setup } = require(process.env.JSDOM_HARNESS);

const conv = { id: 'c1', messages: [
  { _msgId: 'm1', role: 'user', content: 'u1' },
  { _msgId: 'm2', role: 'assistant', content: 'a1' },
  { _msgId: 'm3', role: 'user', content: 'u2' },
  { _msgId: 'm4', role: 'assistant', content: 'a2' },
]};
const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    activeConvId: 'c1',
    conversations: [conv],
    renderMessage: (msg, idx) =>
      '<div class="message" id="msg-' + idx + '" data-msg-id="' + msg._msgId +
      '">' + msg.content + '</div>',
    _ensureMsgId: () => {},
    _convRenderFingerprint: () => 'fp',
    _lastRenderedFingerprint: '',
  },
});

const warns = [];
console.warn = (...a) => { warns.push(a.join(' ')); };

function domSeq() {
  return Array.from(document.querySelectorAll('#chatInner .message'))
    .map(el => el.getAttribute('data-msg-id'));
}
function msgSeq() { return conv.messages.map(m => m._msgId); }
function sameSeq(a, b) {
  return a.length === b.length && a.every((v, i) => v === b[i]);
}

/* send: initial four applies append in order. */
for (let i = 0; i < conv.messages.length; i++) {
  window.ConvView.apply('c1', i, conv.messages[i]);
}
check('after_send_order_matches', sameSeq(domSeq(), msgSeq()));

/* edit: apply on existing mid-list node replaces IN PLACE (no reorder). */
conv.messages[1].content = 'a1-edited';
window.ConvView.apply('c1', 1, conv.messages[1]);
check('after_edit_order_matches', sameSeq(domSeq(), msgSeq()) &&
  document.querySelector('[data-msg-id="m2"]').textContent === 'a1-edited');

/* regen: truncate tail (removeMessage) then push+apply a fresh assistant. */
window.ConvView.removeMessage('c1', conv.messages[3]);
conv.messages.splice(3, 1);
const fresh = { _msgId: 'm4b', role: 'assistant', content: 'a2-fresh' };
conv.messages.push(fresh);
window.ConvView.apply('c1', 3, fresh);
check('after_regen_order_matches', sameSeq(domSeq(), msgSeq()));

/* upsert replace keeps order. */
window.ConvView.upsertMessage('c1', conv.messages[2]);
check('after_upsert_order_matches', sameSeq(domSeq(), msgSeq()));

/* THE ANCHOR: after the whole send/edit/regen flow, DOM seq === doc seq. */
check('ANCHOR_dom_seq_equals_doc_seq', sameSeq(domSeq(), msgSeq()));

/* ③a mid-list append with NO existing node → loud warn (drift surface).
 * NOTE: this step deliberately drifts DOM vs doc (the ghost's idx fallback
 * clobbers msg-1) — that destruction is exactly what the loud warn exists
 * to surface, so the anchor runs BEFORE it. */
const midGhost = { _msgId: 'm2x', role: 'assistant', content: 'drifted' };
conv.messages.splice(1, 0, midGhost);   // mid-list in the doc, absent in DOM
window.ConvView.apply('c1', 1, midGhost);
check('midlist_append_warned',
  warns.some(w => w.indexOf('MID-LIST') >= 0));

report();
