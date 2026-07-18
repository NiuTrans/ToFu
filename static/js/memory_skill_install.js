/* ════════════════════════════════════
   memory_skill_install.js — skill-package (.zip) drag/drop install
   Extracted from memory.js (2026-07). The skill-package install layer:
   _attachMemoryDropZone (OS file drag/drop) + installSkillFromFileInput +
   _uploadSkillPackage (Api.memory.install) + _showInstallToast. A distinct
   concern from memory-card CRUD. Plain window-scope concatenation (NOT an
   IIFE) — _attachMemoryDropZone is called at runtime from openMemoryModal;
   calls back into _buildMemoryCardEl/_updateMemoryStats/_memoryCache in
   memory.js. Load order is free (both before main.js).
   ════════════════════════════════════ */

// ══════════════════════════════════════════════════════
// ★ Skill-Package Install — drag-and-drop .zip into modal
// ══════════════════════════════════════════════════════

let _memoryDropAttached = false;

function _attachMemoryDropZone() {
  if (_memoryDropAttached) return;
  const modal = document.getElementById("memoryModal");
  const card  = modal ? modal.querySelector(".memory-modal") : null;
  if (!modal || !card) return;
  // Shared OS-file .zip drag/drop wiring (core/zip_drop_zone.js). The upload +
  // toast callbacks stay local (memory-specific scope + incremental card render).
  _memoryDropAttached = attachZipDropZone({
    listenEl: modal,
    highlightEl: card,
    onFile: (f) => _uploadSkillPackage(f),
    onReject: () => _showInstallToast(t('memory.notZip'), true),
  });
}

function installSkillFromFileInput(inputEl) {
  const f = inputEl && inputEl.files && inputEl.files[0];
  if (!f) return;
  _uploadSkillPackage(f);
  inputEl.value = "";  // allow re-selecting the same file
}

async function _uploadSkillPackage(file) {
  const activeTab = document.querySelector(".memory-tab.active");
  const tabScope = activeTab?.dataset?.scope;
  const scope = (tabScope === "global") ? "global" : "project";

  _showInstallToast(t('memory.installingFile', { name: file.name }));
  const fd = new FormData();
  fd.append("file", file);
  fd.append("scope", scope);

  try {
    const r = await Api.memory.install(fd);
    const d = (r ? await r.json().catch(() => ({})) : {});
    if (!r || !r.ok) {
      const _err = d.error || (r && r.statusText) || t('memory.noResponse');
      _showInstallToast(t('memory.installFailed', { err: _err }), true);
      debugLog("Skill install failed: " + _err, "error");
      return;
    }

    const mem = d.memory || {};
    const hints = d.install_hints || [];
    let msg = t('memory.installedPackage', { name: mem.name, scope: mem.scope });
    if (d.replaced) msg += t('memory.replacedOld');
    if (hints.length) {
      const files = hints.map(h => h.file).join(", ");
      msg += t('memory.installHintSuffix', { files: files });
    }
    _showInstallToast(msg);
    debugLog(msg, "success");

    // Insert new card on top, no full refresh.
    _memoryCache.unshift(mem);
    const list = document.getElementById("memoryList");
    if (list) {
      if (list.querySelector('.memory-empty')) list.innerHTML = '';
      const cardEl = _buildMemoryCardEl(mem);
      cardEl.style.animation = 'memorySlam .3s cubic-bezier(.2,1,.3,1)';
      list.prepend(cardEl);
    }
    _updateMemoryStats(_memoryCache);
  } catch (e) {
    _showInstallToast(t('memory.installError', { err: e.message }), true);
    debugLog("Skill install error: " + e.message, "error");
  }
}

function _showInstallToast(text, isError) {
  const modal = document.getElementById("memoryModal");
  const card = modal ? modal.querySelector(".memory-modal") : null;
  // Shared fade-out plumbing (core/toast.js::_ephemeralToast); keeps this
  // toast's in-modal-card parent + .memory-install-toast class.
  _ephemeralToast(card, "memory-install-toast" + (isError ? " is-error" : ""),
                  text, isError ? 5000 : 3500);
}
