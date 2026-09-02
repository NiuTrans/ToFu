/* ===== migrated source: ui/preference_actions.js ===== */
/* User actions for preference proposals and learned context changes. */

async function resolvePreference(btn, pendingId, accept) {
  const row = btn && btn.closest ? btn.closest('.pl-row') : null;
  try {
    if (row) { row.style.opacity = '0.5'; row.style.pointerEvents = 'none'; }
    await Api.post(`/api/v1/profile/pending/${encodeURIComponent(pendingId)}`,
                   { accept: !!accept });
    if (row) {
      const translate = (typeof t === 'function') ? t : (key => key);
      row.innerHTML = `<span class="pl-lead">${Icon(accept ? 'check' : 'x', 13)}</span>`
        + `<span class="pl-text">${accept ? translate('prefs.learnedReinforced') : translate('prefs.dismiss')}</span>`;
      row.classList.add('pl-resolved');
      row.style.opacity = '';
    }
  } catch (error) {
    console.warn('[resolvePreference] failed', error);
    if (typeof showToast === 'function') showToast('⚠️', 'Error', String(error), 4000);
    if (row) { row.style.opacity = ''; row.style.pointerEvents = ''; }
  }
}
runtimeScope.resolvePreference = resolvePreference;

async function undoContextChange(btn, changeId) {
  if (!changeId || !btn) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = (typeof t === 'function' ? t('context.undoing') : '撤销中…');
  try {
    await Api.userContext.undo(changeId);
    btn.textContent = (typeof t === 'function' ? t('context.undone') : '已撤销');
    btn.classList.add('is-undone');
  } catch (error) {
    btn.disabled = false;
    btn.textContent = old;
    if (typeof showToast === 'function') {
      showToast('⚠️', (typeof t === 'function' ? t('context.undoFailed') : '撤销失败'),
                String(error.message || error), 4000);
    }
  }
}
runtimeScope.undoContextChange = undoContextChange;
