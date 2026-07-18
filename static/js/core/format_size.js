/* ════════════════════════════════════
   core/format_size.js — human-readable byte size

   De-duplicates the byte→"B/KB/MB" formatter that lived twice byte-identical:
   image-gen.js::_formatFileSize and skills.js::_skillsFmtSize. Both did the
   same B/KB/MB math with .toFixed(1). This is the single shared entry.

   NOT converged: compaction-viewer.js::_fmtBytes stays local — it lives inside
   that file's `'use strict'` IIFE closure (moving it out would force it onto
   window, the anti-pattern) and carries a distinct Number(n)||0 coercion.

   Plain window-scope (no IIFE); load in core/ before its consumers.
   ════════════════════════════════════ */

/**
 * Format a byte count as a short human-readable string (B / KB / MB).
 * Returns '' for a non-positive / missing size (callers gate on truthiness).
 *
 * @param {number} bytes
 * @returns {string}
 */
function formatFileSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}
if (typeof window !== 'undefined') window.formatFileSize = formatFileSize;
