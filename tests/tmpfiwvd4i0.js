
const { setup } = require(process.env.JSDOM_HARNESS);

/* Localized heartbeat labels, byte-copied from static/js/i18n.js so the
 * assertion reads the SAME string production renders. */
const I18N = {
  'stream.phase.waitingFirstByte':
    '已等待 {elapsed}s：{model} 尚未返回首个字节…',
  'stream.phase.retrying': '正在重试…',
};

const { window, document, check, report } = setup({
  root: process.argv[3],
  html: '<!DOCTYPE html><body><div id="chatContainer"><div id="chatInner"></div></div></body>',
  targets: [process.argv[2]],
  globals: {
    isNearBottom: () => true,
    scrollToBottom: () => {},
    _syncToolRoundsDOM: () => {},
    _buildSwarmInboxChipsHTML: () => '',
    Icon: () => '<svg></svg>',
    t: (key, args) => {
      let s = I18N[key];
      if (s === undefined) return key;
      if (args) for (const k of Object.keys(args)) {
        s = s.split('{' + k + '}').join(String(args[k]));
      }
      return s;
    },
  },
});

/* The streaming bubble, with a status zone present (the phase paint target).
 * Seeds every zone updateStreamingUI dereferences unconditionally (thinking
 * and fc are read without a null guard on the paint path). */
document.getElementById('chatInner').innerHTML =
  '<div class="message streaming-message" id="streaming-msg">' +
    '<div class="message-body" id="streaming-body">' +
      '<div data-zone="content" class="stream-content"></div>' +
      '<div data-zone="thinking" class="stream-thinking" style="display:none"></div>' +
      '<div data-zone="tool" class="stream-tool"></div>' +
      '<div data-zone="fc" class="stream-fc"></div>' +
      '<div data-zone="status" class="stream-status"></div>' +
    '</div>' +
  '</div>';
const body = document.getElementById('streaming-body');
const statusZone = body.querySelector('[data-zone="status"]');

/* ── The INGRESS reducer, extracted verbatim from the production source ──
 * We don't eval all of sse_pipeline.js (it needs the whole app substrate);
 * instead we PIN the real whitelist by slicing the `setStreamPhase(convId, {…})`
 * object literal out of the phase branch and building it here. If a future
 * edit drops a field from that literal, this harness picks the drop up. */
const fs = require('fs');
const path = require('path');
const pipelinePath = process.env.PIPELINE_JS
  || path.join(process.argv[3], 'static', 'js', 'ui', 'sse_pipeline.js');
const pipelineSrc = fs.readFileSync(pipelinePath, 'utf-8');
const phaseIdx = pipelineSrc.indexOf('} else if (ev.type === "phase") {');
check('ingress_phase_branch_found', phaseIdx > 0);
const callIdx = pipelineSrc.indexOf('setStreamPhase(convId, {', phaseIdx);
const endIdx = pipelineSrc.indexOf('});', callIdx);
check('ingress_setStreamPhase_call_found', callIdx > 0 && endIdx > callIdx);
const literal = pipelineSrc.slice(
  callIdx + 'setStreamPhase(convId, '.length, endIdx + 1);
/* Build the real reducer from the real literal. */
const reduceIngress = new Function('ev', 'return ' + literal + ';');

/* Exactly what lib/tasks_pkg/manager/_stream.py::_on_waiting emits per beat
 * (FIRST_BYTE_HEARTBEAT_S=20 → attempt = elapsed // 20). */
function heartbeatEvent(elapsedSec) {
  const model = 'yuju-claude-opus-5-evaDaily';
  return {
    type: 'phase',
    phase: 'retrying',
    detail: `Waiting ${elapsedSec}s — no first byte from ${model} yet…`,
    detailKey: 'stream.phase.waitingFirstByte',
    detailArgs: { model, elapsed: elapsedSec },
    attempt: Math.max(1, Math.floor(elapsedSec / 20)),
    model,
  };
}

function beat(elapsedSec) {
  const phase = reduceIngress(heartbeatEvent(elapsedSec));
  updateStreamingUI({ toolRounds: [], content: '', thinking: '', phase });
  return statusZone.textContent || '';
}

/* 1. INGRESS: `attempt` must survive the whitelist — the backend's documented
 *    repaint contract depends on it. */
const reduced = reduceIngress(heartbeatEvent(40));
check('ingress_preserves_attempt', reduced.attempt === 2);
check('ingress_preserves_detailKey',
      reduced.detailKey === 'stream.phase.waitingFirstByte');
check('ingress_preserves_detailArgs_elapsed',
      !!reduced.detailArgs && reduced.detailArgs.elapsed === 40);

/* 2. RENDER: three consecutive beats must each repaint the LIVE seconds. */
const t20 = beat(20);
const t40 = beat(40);
const t60 = beat(60);
check('beat20_painted', t20.indexOf('已等待 20s') >= 0);
check('beat40_repainted_live', t40.indexOf('已等待 40s') >= 0);
check('beat60_repainted_live', t60.indexOf('已等待 60s') >= 0);
/* The frozen-text signature of the bug: still showing the FIRST beat. */
check('beat60_is_not_frozen_on_first_beat', t60.indexOf('已等待 20s') < 0);

/* 3. The sibling latent class: a phase whose detailKey is CONSTANT while its
 *    detailArgs change must still repaint (compacting/exec/working/thinking
 *    all keyed on the raw detailKey before this fix). */
function argOnlyBeat(n) {
  updateStreamingUI({
    toolRounds: [], content: '', thinking: '',
    phase: { phase: 'retrying', detailKey: 'stream.phase.waitingFirstByte',
             detailArgs: { model: 'M', elapsed: n }, attempt: 7 },
  });
  return statusZone.textContent || '';
}
const a1 = argOnlyBeat(80);
const a2 = argOnlyBeat(100);
check('same_attempt_changed_args_still_repaints',
      a1.indexOf('已等待 80s') >= 0 && a2.indexOf('已等待 100s') >= 0);

report();
