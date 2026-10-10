/* ===== migrated source: ui/tool_rounds_rich.js ===== */
/* Retained rich-card composition only. Delete these adapters with core dispatch migration.
 * Typed owners own markup and interaction; core owns authoritative recovery and the shared clock. */
const _checklistCardText = (key, fallback) => typeof t === 'function' ? t(key, fallback) : fallback;
function _renderTodoBlock(round, svg, q, badgeHtml) {
  return renderChecklistCard(round, svg, () => _rowRightControls(round), _checklistCardText);
}
const _timerCardPorts = Object.freeze({
  text: (key, fallback) => typeof t === 'function' ? t(key, fallback) : fallback,
  now: () => Date.now(),
  icon: (name, size) => Icon(name, size),
  markdown: content => typeof renderMarkdown === 'function' ? renderMarkdown(content) : escapeHtml(content),
  toolIcon: name => typeof _TOOL_DISPLAY !== 'undefined' ? _TOOL_DISPLAY[name]?.icon : undefined,
  formatTime: timestamp => new Date(timestamp).toLocaleTimeString(),
  demandCountdown: () => { if (typeof _demandToolElapsedTicker === 'function') _demandToolElapsedTicker(); },
});
function _renderTimerWatcherBlock(round, svg) {
  return renderTimerCard(round, _timerRecoveryPresentation(round), _rowRightControls(round), _timerCardPorts);
}
function _tickTimerCountdowns() {
  return refreshTimerCountdowns(document, _timerCardPorts);
}
function _timerWatcherToggle(el, ev) {
  toggleTimerWatcher(el, ev, _timerCardPorts.demandCountdown);
}
function _timerInstructionToggle(el, ev) {
  toggleTimerInstruction(el, ev);
}
function _timerPollToggle(el, ev) {
  toggleTimerPoll(el, ev);
}

/* Project receipt presentation is owned by typed modules. Delete this bridge
 * with core dispatch migration; it owns no metadata, state or lifecycle. */
const _projectReceiptPorts = Object.freeze({
  text: (key, fallback) => typeof t === 'function' ? t(key, fallback) : fallback,
  markdown: content => renderMarkdown(content),
  icon: (name, size) => typeof Icon === 'function' ? Icon(name, size) : undefined,
  inlineMarkup: content => typeof _tpInlineMd === 'function' ? _tpInlineMd(content) : undefined,
  conversationTitle: id => typeof convTitleById === 'function' ? convTitleById(id) : undefined,
  now: () => Date.now(),
});
function _renderConvMetaBlock(round, svg, q, badgeHtml) {
  return renderProjectReceipt(round, svg, badgeHtml, _projectReceiptPorts);
}

/* Multimedia receipts live in typed motion-card owners; remove this bridge
 * when core dispatch moves to the typed conversation renderer. */
const _motionCardPorts = Object.freeze({
  text: (key, fallback) => typeof t === 'function' ? t(key, fallback) : fallback,
  scanWorkUnits: () => { if (typeof _wuScanSoon === 'function') _wuScanSoon(); },
});
function _renderMotionVideoBlock(round, svg, q, badgeHtml) {
  return renderMotionCard(round, svg, badgeHtml, _motionCardPorts);
}
