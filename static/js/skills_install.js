/* ════════════════════════════════════
   skills_install.js — skill-package (.zip) drag/drop + upload + toast
   Extracted from skills.js (2026-07). The install-transport layer of the
   Skills tab: _skillsAttachDropZone (OS file drag/drop) + _skillsInstallFromInput
   + _skillsUploadZip (Api.skills.install) + _openSkillsStoreFromMemory +
   _skillsToast. Plain window-scope concatenation (NOT an IIFE) —
   _skillsAttachDropZone is called at runtime from _populateSkillsTab; the OS
   drag/drop plumbing is now the shared core/zip_drop_zone.js helper, and the
   upload/toast callbacks call _populateSkillsTab back in skills.js. Load order
   is free (both before main.js). Mirrors memory_skill_install.js.
   ════════════════════════════════════ */

// ── Drag-and-drop & file picker ────────────────────────────────

function _skillsAttachDropZone() {
  if (_skillsDropAttached) return;
  var panel = document.getElementById('settingsTab_skills');
  var zone = document.getElementById('skillsDropZone');
  if (!panel || !zone) return;
  // Shared OS-file .zip drag/drop wiring (core/zip_drop_zone.js). The upload +
  // toast callbacks stay local (skills-specific reload + body toast).
  _skillsDropAttached = attachZipDropZone({
    listenEl: panel,
    highlightEl: zone,
    onFile: function (f) { _skillsUploadZip(f); },
    onReject: function () { _skillsToast(t('skills.notZip'), 'error'); },
  });
}

function _skillsInstallFromInput(input) {
  var f = input && input.files && input.files[0];
  if (!f) return;
  _skillsUploadZip(f);
  input.value = '';
}

async function _skillsUploadZip(file) {
  _skillsToast(t('skills.installingFile', { name: file.name }));
  var fd = new FormData();
  fd.append('file', file);
  fd.append('scope', 'project');
  try {
    var r = await Api.skills.install(fd);
    var d = (r ? await r.json().catch(function () { return {}; }) : {});
    if (!r || !r.ok) {
      _skillsToast(t('skills.installFailed', { err: (d.error || (r && r.statusText) || t('skills.noResponse')) }), 'error');
      return;
    }
    var hints = d.install_hints || [];
    var msg = t('skills.installedToast', { name: d.memory.name });
    if (hints.length) msg += t('skills.installHintSuffixUpload', { files: hints.map(function (h) { return h.file; }).join(', ') });
    _skillsToast(msg, 'success');
    await _populateSkillsTab();
  } catch (e) {
    _skillsToast(t('skills.installError', { err: e.message }), 'error');
  }
}

// ── Toast helper ──────────────────────────────────────────────

// ── Cross-modal entry: open Settings → Skills tab from Memory modal ──

function _openSkillsStoreFromMemory() {
  // Close memory modal first
  if (typeof closeMemoryModal === 'function') closeMemoryModal();
  // Open settings, then switch to Skills tab once it's open
  if (typeof openSettings === 'function') {
    openSettings();
    setTimeout(function () {
      if (typeof switchSettingsTab === 'function') switchSettingsTab('skills');
    }, 50);
  }
}

function _skillsToast(text, kind) {
  // Shared fade-out plumbing (core/toast.js::_ephemeralToast); keeps this
  // toast's document.body parent + 3-state .skills-toast class.
  var cls = 'skills-toast' + (kind === 'error' ? ' is-error' : kind === 'success' ? ' is-success' : '');
  _ephemeralToast(document.body, cls, text, kind === 'error' ? 5000 : 3500);
}
