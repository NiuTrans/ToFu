
const fs = require('fs');
const path = require('path');
const ROOT = process.argv[3];
const { JSDOM } = require(path.join(ROOT, 'node_modules', 'jsdom'));
const dom = new JSDOM(
  '<!DOCTYPE html><body><div class="conv-status-strip" id="convStatusStrip">' +
  '<div class="turn-nav" id="turnNav"></div></div></body>',
  { url: 'http://localhost/' });
const win = dom.window;
global.window = win;
global.document = win.document;
Object.defineProperty(win.document, 'hidden', { value: false, configurable: true });
global.requestAnimationFrame = (cb) => setTimeout(cb, 0);
global.runtimeScope = win.runtimeScope = {};
global.activeConvId = 'conv-1';

// ── Controllable Api stub ──
let SNAPSHOT = { swarms: [], backgroundCommands: [] };
global.Api = win.Api = {
  activity: { background: () => Promise.resolve(SNAPSHOT) },
};
let loadedConv = null;
global.getConvById = win.getConvById =
  (id) => (id === 'conv-1' ? { id: 'conv-1', title: 'Refactor session' } : null);
global.loadConversation = win.loadConversation = (id) => { loadedConv = id; };

eval(fs.readFileSync(process.argv[2], 'utf8'));  // background-activity.js

const out = [];
function check(name, cond) { out.push((cond ? 'PASS ' : 'FAIL ') + name); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const ACTIVE = {
  swarms: [{
    swarm_key: 'conv-1', conv_id: 'conv-1',
    running: 1, pending: 1, completed: 2, agent_count: 2,
    agents: [
      { id: 'a1', role: 'coder', objective: 'Rewrite the <b>parser</b>',
        status: 'running', round: 3, error: '' },
      { id: 'a2', role: 'reviewer', objective: 'Audit the diff',
        status: 'pending', round: 0, error: '' },
    ],
    created_at: Date.now() / 1000 - 65,
  }],
  backgroundCommands: [{
    command_id: 'bg_abc', conv_id: 'conv-1', command: 'npm run build --workspace=x',
    status: 'running', started_at: Date.now() / 1000 - 10,
    elapsed_s: 10, delivery_status: 'not_applicable',
  }],
};

(async () => {
  const strip = document.getElementById('convStatusStrip');
  // Chip mounts lazily on the first applied snapshot — force a tick while
  // still empty: it must stay hidden AND stay inside the strip.
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  const chip = document.getElementById('bgActivityChip');
  check('chip_in_strip', !!chip && chip.parentElement === strip);
  check('chip_before_turnnav',
        !!chip && chip.nextElementSibling === document.getElementById('turnNav'));
  check('chip_hidden_when_idle', !!chip && chip.style.display === 'none');

  // Active snapshot → chip visible with the combined label.
  SNAPSHOT = ACTIVE;
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  check('chip_visible_when_active', chip.style.display !== 'none');
  const label = chip.querySelector('.bg-act-label').textContent;
  check('chip_label_agents', label.includes('1 sub-agent running'));
  check('chip_label_queued', label.includes('1 sub-agent waiting'));
  check('chip_label_commands', label.includes('1 background command running'));

  // Popover: sections + rows render, objectives/commands as TEXT.
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  const pop = document.getElementById('bgActivityPopover');
  check('popover_opens', !!pop);
  check('popover_sections',
        !!pop && pop.textContent.includes('Sub-agents') &&
        pop.textContent.includes('Background commands'));
  check('popover_conv_title',
        !!pop && pop.textContent.includes('Refactor session'));
  check('agent_role_rendered',
        !!pop && !!pop.querySelector('.bg-act-agent-role') &&
        pop.querySelector('.bg-act-agent-role').textContent === 'coder');
  check('agent_round_shown',
        !!pop && pop.querySelector('.bg-act-agent-status').textContent
                     .includes('round 3'));
  check('command_rendered',
        !!pop && pop.textContent.includes('npm run build --workspace=x'));
  check('objective_is_text_not_markup',
        !!pop && pop.querySelectorAll('.bg-act-agent-objective b').length === 0 &&
        pop.querySelector('.bg-act-agent-objective').textContent
            .includes('<b>parser</b>'));

  const idButton = pop.querySelector('.bg-act-agent-id');
  check('full_log_id', idButton.textContent === 'agent-coder-a1');
  check('canonical_address', idButton.closest('.bg-act-agent').dataset.agentId === 'a1');
  let copied = '';
  Object.defineProperty(win.navigator, 'clipboard', { configurable: true,
    value: { writeText: async value => { copied = value; } } });
  idButton.focus();
  idButton.click();
  await sleep(0);
  check('copy_full_id', copied === 'agent-coder-a1');
  check('copy_does_not_navigate', loadedConv === null && !!document.getElementById('bgActivityPopover'));
  check('copy_keeps_focus', document.activeElement === idButton);
  check('copy_feedback', idButton.title === 'Copied!');
  win.navigator.clipboard.writeText = async () => { throw Error('denied'); };
  idButton.click();
  await sleep(0);
  check('copy_failure_visible', idButton.title === 'Copy failed');

  // Deep-link: swarm row click loads its conversation and closes the popover.
  pop.querySelector('.bg-act-swarm').dispatchEvent(
    new win.MouseEvent('click', { bubbles: true }));
  check('swarm_deeplinks', loadedConv === 'conv-1');
  check('popover_closed_after_deeplink',
        !document.getElementById('bgActivityPopover'));

  // Navigation reprojects synchronously, including an outstanding old poll.
  chip.dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  let resolveOld;
  Api.activity.background = () => new Promise(resolve => { resolveOld = resolve; });
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  global.activeConvId = 'conv-2';
  runtimeScope.updateBackgroundActivity();
  check('other_conversation_hidden_immediately', chip.style.display === 'none');
  check('old_popover_closed_on_switch', !document.getElementById('bgActivityPopover'));
  resolveOld(ACTIVE);
  await sleep(60);
  check('late_response_cannot_leak', chip.style.display === 'none');
  Api.activity.background = () => Promise.resolve(SNAPSHOT);
  global.activeConvId = 'conv-1';
  runtimeScope.updateBackgroundActivity();
  check('return_restores_current_activity', chip.style.display !== 'none');
  global.activeConvId = null;
  runtimeScope.updateBackgroundActivity();
  check('new_chat_hidden_immediately', chip.style.display === 'none');
  global.activeConvId = 'conv-1';

  SNAPSHOT = { swarms: [{ ...ACTIVE.swarms[0], running: 2, pending: 0,
    agents: ACTIVE.swarms[0].agents.map(a => ({...a, status: 'pending', round: 0, waitReason: 'capacity'}))
  }], backgroundCommands: [] };
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  check('queued_only_visible', chip.style.display !== 'none');
  check('queued_only_not_running', chip.querySelector('.bg-act-label').textContent === '2 sub-agents waiting');
  chip.click();
  let waitingPop = document.getElementById('bgActivityPopover');
  check('same_roster_summary', waitingPop.querySelector('.bg-act-swarm-summary').textContent === '2 waiting');
  check('no_zero_count_noise', !waitingPop.querySelector('.bg-act-swarm-summary').textContent.includes('0'));
  check('capacity_wait_explained', [...waitingPop.querySelectorAll('.bg-act-agent-status')]
    .every(el => el.textContent === 'Waiting for capacity'));
  SNAPSHOT.swarms[0].agents[1].waitReason = 'dependencies';
  SNAPSHOT.swarms[0].agents[1].dependsOn = ['a1'];
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  chip.click();
  waitingPop = document.getElementById('bgActivityPopover');
  check('wait_reason_refreshes_without_count_change', waitingPop.querySelectorAll('.bg-act-agent-status')[1]
    .textContent === 'Waiting for prerequisites');
  check('dependency_is_inspectable', waitingPop.querySelectorAll('.bg-act-agent-status')[1].title === 'a1');
  SNAPSHOT.swarms[0].agents = SNAPSHOT.swarms[0].agents.map(a => ({...a, status: 'running', round: 2}));
  // Stale aggregate fields can no longer disagree with the actual roster.
  SNAPSHOT.swarms[0].running = 0; SNAPSHOT.swarms[0].pending = 2;
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  chip.click();
  waitingPop = document.getElementById('bgActivityPopover');
  check('running_summary_is_compact', waitingPop.querySelector('.bg-act-swarm-summary').textContent === '2 running');
  check('running_roster_not_waiting', [...waitingPop.querySelectorAll('.bg-act-agent-status')]
    .every(el => el.textContent === 'running · round 2'));

  // Snapshot clears → chip hides again.
  SNAPSHOT = { swarms: [], backgroundCommands: [] };
  runtimeScope.updateBackgroundActivity();
  await sleep(60);
  check('chip_hides_when_cleared', chip.style.display === 'none');

  console.log(out.join('\n'));
  process.exit(0);
})();
