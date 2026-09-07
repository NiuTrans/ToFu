/* ===== migrated source: ui/turn_nav.js ===== */
/* Stable TurnStore navigation for the conversation rail.
 *
 * Responsibility: project main-lane input Turns into navigation buttons and
 * scroll to Surface-owned data-turn-id nodes. It never reads the compatibility
 * message array and never writes below #chatInner.
 */

let _turnNavFingerprint = '';
let _lastActiveTurnId = '';

function _turnNavState(conv) {
  return conv?.id
    ? runtimeScope.ConversationTurnStore?.ensureRuntimeStore?.(conv.id)?.getState?.()
    : null;
}

function _turnNavInputs(state) {
  const order = state?.laneOrder?.main || [];
  return order.map((turnId) => state.turnsById?.[turnId])
    .filter((turn) => turn
      && ['human', 'critic', 'virtual_user'].includes(turn.actor));
}

function _turnNavPreview(turn) {
  const projection = turn?.projection || {};
  const direct = String(projection.content || '').trim();
  if (direct) return direct.split('\n')[0].slice(0, 40);
  const segment = (projection.segments || []).find((item) =>
    item && item.type === 'text' && item.text);
  return String(segment?.text || '').trim().split('\n')[0].slice(0, 40);
}

function _turnNavWriteFiles(state, inputTurn) {
  const order = state?.laneOrder?.[inputTurn.laneId || 'main'] || [];
  const start = order.indexOf(inputTurn.turnId);
  const files = new Set();
  let hasWrites = false;
  for (const turnId of order.slice(start + 1)) {
    const turn = state.turnsById?.[turnId];
    if (!turn) continue;
    if (['human', 'critic', 'virtual_user'].includes(turn.actor)) break;
    const projection = turn.projection || {};
    const lists = [
      projection.modifiedFileList,
      projection.fileChanges?.files,
    ];
    for (const list of lists) {
      for (const file of Array.isArray(list) ? list : []) {
        const path = typeof file === 'string' ? file : file?.path;
        if (path) files.add(String(path).split('/').pop());
      }
    }
    if (Number(projection.modifiedFiles || 0) > 0 || files.size) hasWrites = true;
  }
  return { hasWrites, files: [...files] };
}

function _turnNavNode(turnId) {
  const inner = document.getElementById('chatInner');
  if (!inner || !turnId) return null;
  return Array.from(inner.querySelectorAll('[data-turn-id]')).find(
    (node) => node.dataset.turnId === turnId,
  ) || null;
}

function _scrollTurnNode(turnId) {
  const node = _turnNavNode(turnId);
  if (!node) return false;
  node.scrollIntoView({
    behavior: typeof prefersReducedMotion === 'function' && prefersReducedMotion()
      ? 'auto' : 'smooth',
    block: 'start',
  });
  return true;
}

function scrollToTurn(turnId) {
  const stableTurnId = typeof turnId === 'string' ? turnId : '';
  if (!stableTurnId) return;
  if (_scrollTurnNode(stableTurnId)) return;
  const conv = typeof getActiveConv === 'function' ? getActiveConv() : null;
  if (!conv) return;
  runtimeScope.requestAuthoritativeConversationRender(
    conv.id, { forceScroll: false },
  );
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!_scrollTurnNode(stableTurnId)) {
        console.warn('[turnNav] Turn node unavailable after convergence:', stableTurnId);
      }
    }));
  }
}

function _wireTurnNav(nav) {
  if (nav.dataset.turnNavWired === 'true') return;
  nav.dataset.turnNavWired = 'true';
  nav.addEventListener('click', (event) => {
    const button = event.target?.closest?.('.turn-dot[data-turn-id]');
    if (button && nav.contains(button)) scrollToTurn(button.dataset.turnId || '');
  });
}

function buildTurnNav(conv) {
  const nav = document.getElementById('turnNav');
  if (!nav) return;
  _wireTurnNav(nav);
  const state = _turnNavState(conv);
  const turns = _turnNavInputs(state);
  const fingerprint = `${conv?.id || ''}:${state?.conversationRevision || 0}:` +
    turns.map((turn) => `${turn.turnId}:${turn.projectionRevision}`).join('|');
  if (fingerprint === _turnNavFingerprint) return;
  _turnNavFingerprint = fingerprint;
  _lastActiveTurnId = '';
  nav.replaceChildren();
  if (turns.length < 2) return;
  const label = nav.ownerDocument.createElement('div');
  label.className = 'turn-nav-label';
  label.textContent = 'Turns';
  nav.appendChild(label);
  turns.forEach((turn, index) => {
    const writes = _turnNavWriteFiles(state, turn);
    const button = nav.ownerDocument.createElement('button');
    button.type = 'button';
    button.className = 'turn-dot';
    if (turn.actor === 'critic') button.classList.add('turn-dot-critic');
    if (writes.hasWrites) button.classList.add('turn-dot-writes');
    button.dataset.turnId = turn.turnId;
    button.textContent = String(index + 1);
    const preview = _turnNavPreview(turn);
    const writeDetail = writes.files.length ? ` ${writes.files.join(', ')}` : '';
    button.title = `Turn ${index + 1}: ${preview}${writeDetail}`;
    nav.appendChild(button);
  });
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(updateActiveTurn);
  }
}

function updateActiveTurn() {
  const nav = document.getElementById('turnNav');
  const container = typeof _getChatContainer === 'function'
    ? _getChatContainer() : document.getElementById('chatContainer');
  if (!nav || !container) return;
  const dots = Array.from(nav.querySelectorAll('.turn-dot[data-turn-id]'));
  if (!dots.length) return;
  const boundary = container.getBoundingClientRect().top
    + container.getBoundingClientRect().height * 0.3;
  let active = dots[0];
  for (const dot of dots) {
    const node = _turnNavNode(dot.dataset.turnId || '');
    if (!node) continue;
    if (node.getBoundingClientRect().top <= boundary) active = dot;
    else break;
  }
  const activeTurnId = active.dataset.turnId || '';
  if (activeTurnId === _lastActiveTurnId) return;
  nav.querySelector('.turn-dot.active')?.classList.remove('active');
  active.classList.add('active');
  _lastActiveTurnId = activeTurnId;
}
