/* ═══════════════════════════════════════════════════════════════════
   settings/system_prompt_editor.js — secondary-window system-prompt editor

   The General tab keeps only a compact summary row + an "Edit…" button.
   The long system prompt itself lives in this nested modal so it doesn't
   bloat the settings panel.

   Source of truth is the hidden #settingSystem textarea — saveSettings()
   reads it into config.systemPrompt, and openSettings() seeds it from
   config. This module only mirrors that textarea into the editor modal
   and writes the edited value back on Apply.

   Concatenated by lib/js_bundler.py — shares window scope with the rest
   of static/js/*.js. No imports/exports.
   ═══════════════════════════════════════════════════════════════════ */

/** Refresh the compact summary line on the General tab from #settingSystem. */
function _refreshSystemPromptSummary() {
  var ta = document.getElementById('settingSystem');
  var summary = document.getElementById('settingSystemSummary');
  if (!ta || !summary) return;
  var val = (ta.value || '').trim();
  if (!val) {
    summary.textContent = (typeof t === 'function')
      ? t('settings.systemPromptEmpty') : '(no custom system prompt set)';
    summary.classList.remove('has-value');
  } else {
    var chars = val.length;
    var firstLine = val.split('\n')[0].slice(0, 60);
    var label = (typeof t === 'function')
      ? t('settings.systemPromptSet') : 'Custom prompt set';
    summary.textContent = label + ' · ' + chars + ' chars · “' + firstLine
      + (val.length > firstLine.length ? '…' : '') + '”';
    summary.classList.add('has-value');
  }
}

/** Current injection mode from the General-tab dropdown ('append'|'replace'). */
function _systemPromptMode() {
  var sel = document.getElementById('settingSystemPromptMode');
  return (sel && sel.value === 'replace') ? 'replace' : 'append';
}

/** In append mode the editor holds only your *additions* — loading the full
 *  built-in default would duplicate the base prompt on top of itself, so the
 *  "Load built-in default" action is disabled. Replace mode customizes the
 *  base, where loading the default is the natural starting point. */
function _syncSystemPromptEditorMode() {
  var mode = _systemPromptMode();
  var loadBtn = document.getElementById('sysPromptLoadDefaultBtn');
  var hint = document.getElementById('sysPromptEditorHint');
  if (loadBtn) {
    loadBtn.disabled = (mode === 'append');
    loadBtn.title = (mode === 'append')
      ? ((typeof t === 'function') ? t('settings.systemPromptLoadDefaultDisabled')
          : 'Only available in Replace mode — in Append mode your text is added on top of the built-in prompt.')
      : '';
  }
  if (hint) {
    var key = (mode === 'append')
      ? 'settings.systemPromptEditorHintAppend'
      : 'settings.systemPromptEditorHintReplace';
    if (typeof t === 'function') hint.textContent = t(key);
  }
}

function openSystemPromptEditor() {
  var src = document.getElementById('settingSystem');
  var area = document.getElementById('sysPromptEditorArea');
  if (!src || !area) return;
  area.value = src.value || '';
  var status = document.getElementById('sysPromptEditorStatus');
  if (status) status.textContent = '';
  _syncSystemPromptEditorMode();
  document.getElementById('sysPromptModal').classList.add('open');
  setTimeout(function () { area.focus(); }, 50);
}

function closeSystemPromptEditor() {
  document.getElementById('sysPromptModal').classList.remove('open');
}

/** Write the editor content back to the hidden textarea (does NOT persist —
 *  saveSettings() does that when the user saves the settings panel). */
function applySystemPromptEditor() {
  var src = document.getElementById('settingSystem');
  var area = document.getElementById('sysPromptEditorArea');
  if (src && area) src.value = area.value;
  _refreshSystemPromptSummary();
  closeSystemPromptEditor();
}

/** Fetch the built-in default prompt and load it into the editor. Shapes the
 *  preview to the user's likely mode: tools on, project off (the common
 *  chat case). */
async function loadDefaultSystemPrompt() {
  var area = document.getElementById('sysPromptEditorArea');
  var status = document.getElementById('sysPromptEditorStatus');
  if (!area) return;
  if (_systemPromptMode() === 'append') return;  // disabled in append mode
  var loadingMsg = (typeof t === 'function')
    ? t('settings.systemPromptLoading') : 'Loading built-in prompt…';
  if (status) status.textContent = loadingMsg;
  try {
    var data = await Api.serverConfig.defaultSystemPrompt(false, true);
    if (data && data.prompt) {
      var confirmReplace = true;
      if (area.value && area.value.trim()) {
        confirmReplace = (typeof showConfirm === 'function')
          ? await showConfirm(
              (typeof t === 'function') ? t('settings.systemPromptOverwriteConfirm')
                : 'Replace the current editor content with the built-in default?',
              { danger: false })
          : true;
      }
      if (confirmReplace) {
        area.value = data.prompt;
        if (status) {
          status.textContent = (typeof t === 'function')
            ? t('settings.systemPromptLoaded') : 'Built-in prompt loaded';
        }
      } else if (status) {
        status.textContent = '';
      }
    } else if (status) {
      status.textContent = (typeof t === 'function')
        ? t('settings.systemPromptLoadFailed') : 'Failed to load built-in prompt';
    }
  } catch (e) {
    if (typeof debugLog === 'function') {
      debugLog('[sysPromptEditor] loadDefault failed: ' + (e && e.message), 'error');
    }
    if (status) {
      status.textContent = (typeof t === 'function')
        ? t('settings.systemPromptLoadFailed') : 'Failed to load built-in prompt';
    }
  }
}

if (typeof window !== 'undefined') {
  window.openSystemPromptEditor = openSystemPromptEditor;
  window.closeSystemPromptEditor = closeSystemPromptEditor;
  window.applySystemPromptEditor = applySystemPromptEditor;
  window.loadDefaultSystemPrompt = loadDefaultSystemPrompt;
  window._refreshSystemPromptSummary = _refreshSystemPromptSummary;
  window._syncSystemPromptEditorMode = _syncSystemPromptEditorMode;
}
