/* ===== migrated source: core/zip_drop_zone.js ===== */
/* ════════════════════════════════════
   core/zip_drop_zone.js — shared OS-file .zip drag/drop wiring

   De-duplicates the byte-identical drop-zone plumbing that lived twice:
   _attachMemoryDropZone (memory_skill_install.js) and _skillsAttachDropZone
   (skills_install.js). Both wired the SAME four listeners (dragenter/dragover/
   dragleave/drop), the SAME OS-files-only guard (dataTransfer.types⊃"Files",
   so intra-app card drags are ignored), the SAME nested-drag depth counter,
   and the SAME "install the first .zip, else reject" drop handler — differing
   ONLY in which element listens, which element gets the .is-dragging highlight,
   and the onFile / onReject callbacks.

   The UPLOAD + TOAST halves are deliberately NOT converged: they genuinely
   diverge (memory: per-tab global/project scope + incremental card prepend +
   memory.* i18n + in-card toast; skills: hardcoded project scope + full
   _populateSkillsTab reload + skills.* i18n + body toast). Converging those
   would need ~5 callbacks — a net complexity increase, not a reduction.

   Plain window-scope (no IIFE); load in core/ before its consumers.
   ════════════════════════════════════ */

/**
 * Wire OS-file .zip drag/drop onto a panel.
 *
 * @param {object} [opts]
 *   @param {Element} [opts.listenEl]   element that receives the drag events
 *   @param {Element} [opts.highlightEl] element toggled with `.is-dragging`
 *   @param {function(File): any} [opts.onFile]   called with the first dropped .zip
 *   @param {function(): any} [opts.onReject]   called when a drop had no .zip
 * @returns {boolean} true if wired, false if elements were missing.
 */
function attachZipDropZone(opts) {
  opts = opts || {};
  var listenEl = opts.listenEl;
  var highlightEl = opts.highlightEl;
  var onFile = opts.onFile;
  var onReject = opts.onReject;
  if (!listenEl || !highlightEl || typeof onFile !== 'function') return false;

  var depth = 0;

  // Only react to an OS drag OF FILES — dataTransfer.types contains "Files".
  // This avoids swallowing intra-app drags of the app's own cards.
  var hasFiles = function (e) {
    var dt = e.dataTransfer;
    if (!dt || !dt.types) return false;
    for (var i = 0; i < dt.types.length; i++) {
      if (dt.types[i] === 'Files') return true;
    }
    return false;
  };

  var isZip = function (f) {
    return !!f && (/\.zip$/i.test(f.name) || f.type === 'application/zip' ||
      f.type === 'application/x-zip-compressed');
  };

  listenEl.addEventListener('dragenter', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    highlightEl.classList.add('is-dragging');
  });
  listenEl.addEventListener('dragover', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  listenEl.addEventListener('dragleave', function (e) {
    if (!hasFiles(e)) return;
    depth = Math.max(0, depth - 1);
    if (depth === 0) highlightEl.classList.remove('is-dragging');
  });
  listenEl.addEventListener('drop', function (e) {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth = 0;
    highlightEl.classList.remove('is-dragging');
    var files = e.dataTransfer.files;
    if (!files || !files.length) return;
    for (var i = 0; i < files.length; i++) {
      if (isZip(files[i])) { onFile(files[i]); return; }
    }
    if (typeof onReject === 'function') onReject();
  });
  return true;
}
if (typeof window !== 'undefined') runtimeScope.attachZipDropZone = attachZipDropZone;

