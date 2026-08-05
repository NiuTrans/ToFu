
const { setup } = require(process.env.JSDOM_HARNESS);

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    isNearBottom: () => true,
    scrollToBottom: () => {},
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
  },
});

/* The lazy-create PRECONDITION, hardcoded: the production default-shape
 * streaming bubble (HEAD _streamingBubbleHTML, default status + no detail)
 * seeds these zones and NO [data-zone="status"]. The tool zone's presence is
 * what makes _ensureStreamZones early-return and leaves the status zone to
 * the lazy-create branch — pin BOTH halves of the precondition. */
const inner = document.getElementById('chatInner');
function seedBubble() {
  inner.innerHTML =
    '<div class="message streaming-message" id="streaming-msg">' +
      '<div class="message-body" id="streaming-body">' +
        '<div data-zone="content" class="stream-content"></div>' +
        '<div data-zone="thinking" class="stream-thinking" style="display:none"></div>' +
        '<div data-zone="tool" class="stream-tool"></div>' +
        '<div data-zone="fc" class="stream-fc"></div>' +
        '<div data-zone="swarmInbox" class="stream-swarm-inbox"></div>' +
      '</div>' +
    '</div>';
  return document.getElementById('streaming-body');
}
const body = seedBubble();
check('fixture_matches_lazy_create_precondition',
      !body.querySelector('[data-zone="status"]')
      && !!body.querySelector('[data-zone="tool"]'));

function frame(n) {
  updateStreamingUI({
    toolRounds: [], thinking: 'x'.repeat(n), content: '',
    phase: { phase: 'thinking_active', _thinkingLen: n },
  });
}

/* 1. THREE frames of ONE live turn (the screenshot's 推理中 3/100/142 字符). */
frame(3); frame(100); frame(142);
const zones = body.querySelectorAll('[data-zone="status"]');
check('single_status_zone_after_3_frames', zones.length === 1);
const ctr = body.querySelector('.stream-phase-counter');
check('sole_zone_is_the_live_one_counter_latest',
      !!ctr && ctr.textContent.indexOf(':142') >= 0);
check('sole_zone_painted_with_thinking_phase',
      zones.length === 1 && zones[0].innerHTML.indexOf('stream-phase') >= 0);

/* 2. Fresh bubble (cache re-derive on a new #streaming-body): two more frames
 *    must STILL yield exactly one status zone. */
const body2 = seedBubble();
frame(175); frame(239);
check('single_status_zone_fresh_bubble',
      body2.querySelectorAll('[data-zone="status"]').length === 1);

report();
