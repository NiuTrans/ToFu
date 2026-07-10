/* ═══════════════════════════════════════════════════════════════════
   image_fullscreen — shared image viewer helpers (CORE bundle).

   `_openImageFullscreen(src)` and `_downloadGenImage(btn)` are invoked via
   inline onclick= from image thumbnails rendered in the CORE bundle:
   the tool-call panel (ui/tool_rounds.js — read_files / inspect_image /
   browser_screenshot thumbnails) and chat_render.js (image-gen result
   cards), plus paper-reader.js.

   They previously lived in image-gen.js, which was moved to the DEFERRED
   feature bundle (loaded only on first entry into Image-Gen mode). That
   left every tool-panel thumbnail's onclick pointing at an undefined
   function until/unless Image-Gen mode was opened — clicking "enlarge"
   silently did nothing. These helpers have no image-gen-specific
   dependencies (only DOM APIs), so they belong in the always-loaded core
   bundle. See lib/js_bundler.py (_BUNDLE_FILES) + index.html.

   This file is concatenated by lib/js_bundler.py — symbols share the same
   window scope as every other static/js/*.js file. No exports/imports.
   ═══════════════════════════════════════════════════════════════════ */

/**
 * Open an image in a fullscreen overlay. Called from tool-result / chat
 * image click handlers.
 */
function _openImageFullscreen(src) {
  // Remove existing
  document.querySelectorAll(".imagegen-fullscreen").forEach((el) => el.remove());
  const overlay = document.createElement("div");
  overlay.className = "imagegen-fullscreen";
  overlay.onclick = (e) => { if (e.target === overlay) overlay.remove(); };
  const img = document.createElement("img");
  img.src = src;
  img.onload = function() {
    // For tall images (aspect ratio > 1.3:1 height:width), allow scrolling
    // instead of shrinking via max-height — avoids the "shows less than
    // the inline version" effect for paper figures.
    if (this.naturalHeight > this.naturalWidth * 1.3) {
      this.style.maxHeight = 'none';
      overlay.style.overflowY = 'auto';
      overlay.style.alignItems = 'flex-start';
      overlay.style.padding = '20px 0';
    }
  };
  overlay.appendChild(img);
  document.body.appendChild(overlay);
  const handler = (e) => {
    if (e.key === "Escape") {
      overlay.remove();
      document.removeEventListener("keydown", handler);
    }
  };
  document.addEventListener("keydown", handler);
}

/**
 * Download a generated image from a tool result card.
 */
function _downloadGenImage(btn) {
  const card = btn.closest(".imagegen-card") || btn.closest(".ig-result-card");
  if (!card) return;
  const img = card.querySelector("img");
  if (!img) return;
  const a = document.createElement("a");
  a.href = img.src;
  a.download = `generated_${Date.now()}.png`;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}
