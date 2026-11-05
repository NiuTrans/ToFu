/* ===== migrated source: upload_preview.js ===== */
/* ════════════════════════════════════
   upload_preview.js — attachment preview modals
   Extracted from upload.js (2026-07). The preview/modal DOM layer:
   previewPendingImage/PdfText, openImagePreview,
   openTextPreview, closePreview + the truncation-bar click delegation.
   (The per-tool-row "model view" button + its delegations were removed on
   2026-07-28 per owner directive — the round-scoped debug panel covers it.)
   Plain window-scope concatenation (NOT an
   IIFE) — called at runtime from action handlers, the composer, and typed
   ConversationSurface adapters;
   load order is free (before main.js). Uses global escapeHtml / getActiveConv /
   getToolRoundsFromMsg / _getToolDisplay / _renderToolGroupsHTML / Icon.
   ════════════════════════════════════ */

// ══════════════════════════════════════════════════════
//  Preview functions
// ══════════════════════════════════════════════════════
function previewPendingImage(i) {
  const img = pendingImages[i];
  if (!img || !img.preview) return;
  openImagePreview(img.preview);
}
// Preview text stays server-side after the unified attachment ingest; fetch
// the parsed chunks on demand instead of relying on a client-side copy.
const _PREVIEW_TEXT_CAP = 200000;
async function previewPendingPdfText(i) {
  const pdf = pendingPdfTexts[i];
  if (!pdf) return;
  const _t = (typeof t === "function") ? t : (k, d) => d;
  // An archive chip has no extracted-text body to preview — its value is the
  // on-disk tree. Open the project browser at the extracted folder instead.
  if (pdf._archive) {
    if (pdf._path && typeof browseDirectory === 'function') {
      try { browseDirectory(pdf._path); } catch (_e) { /* best-effort */ }
    }
    return;
  }
  const sizeStr =
    pdf.textLength >= 1024
      ? `${(pdf.textLength / 1024).toFixed(1)}KB`
      : `${pdf.textLength} chars`;
  const title = `📄 ${pdf.name}`;
  const meta = `${pdf.pages} pages · ${sizeStr}`;
  let text = pdf.text || "";
  if (String(text).trim() || !pdf.attachmentId ||
      !(typeof Api !== "undefined" && Api.knowledge && Api.knowledge.content)) {
    openTextPreview(title, meta, text);
    return;
  }
  openTextPreview(title, meta, _t("knowledge.contentLoading", "Loading…"));
  try {
    const data = await Api.knowledge.content(pdf.attachmentId, 0, 200);
    const chunks = (data && data.chunks) || [];
    text = chunks
      .map((c) => {
        const section = String((c && c.section) || "").trim();
        const content = String((c && c.content) || "");
        return section ? `[${section}]\n${content}` : content;
      })
      .filter((part) => part.trim())
      .join("\n\n");
    if (text.length > _PREVIEW_TEXT_CAP)
      text = `${text.slice(0, _PREVIEW_TEXT_CAP)}\n[preview truncated]`;
  } catch (e) {
    console.warn("[Preview] content fetch failed:", e && e.message);
    text = "";
  }
  openTextPreview(title, meta, text);
}

/* The overlay is a viewer, not a poster. Fitting the whole bitmap into the
 * viewport made a tall page render SMALLER than the same image in the message
 * body, so "enlarge" read as a zoom-out and there was no way back to 1:1. Two
 * explicit modes now exist:
 *   natural — as wide as the viewport allows, never upscaled above 1:1; a
 *             taller-than-viewport bitmap scrolls instead of shrinking.
 *   whole   — the entire bitmap inside the viewport, for an overview.
 * Click/Enter toggles between them, so the clicked image is always at least
 * as large as the inline render and 1:1 is always reachable. */
function _previewImageViewport() {
  return {
    width: Math.max(120, Math.round((window.innerWidth || 1200) * 0.92)),
    height: Math.max(120, Math.round((window.innerHeight || 900) * 0.86)),
  };
}
function _previewImageResize(image, scale) {
  image.style.width = `${Math.round((image.naturalWidth || 0) * scale)}px`;
  image.style.height = `${Math.round((image.naturalHeight || 0) * scale)}px`;
}
function _previewImageNatural(image) {
  const naturalWidth = image.naturalWidth || 0;
  if (!naturalWidth || !(image.naturalHeight || 0)) return;
  const viewport = _previewImageViewport();
  _previewImageResize(image, Math.min(1, viewport.width / naturalWidth));
  image.classList.remove("preview-image--whole");
}
function _previewImageWhole(image) {
  const naturalWidth = image.naturalWidth || 0;
  const naturalHeight = image.naturalHeight || 0;
  if (!naturalWidth || !naturalHeight) return;
  const viewport = _previewImageViewport();
  _previewImageResize(image, Math.min(
    1, viewport.width / naturalWidth, viewport.height / naturalHeight,
  ));
  image.classList.add("preview-image--whole");
}
function togglePreviewImageZoom(image) {
  if (image.classList.contains("preview-image--whole")) _previewImageNatural(image);
  else _previewImageWhole(image);
}
function openImagePreview(src) {
  if (!src) return;
  const body = document.getElementById("previewBody");
  const modal = document.getElementById("previewModal");
  if (!body || !modal) return;
  /* Reuse the close button across opens. Rebuilding it would remove the
   * currently focused node, and the browser's focus fixup then lands on the
   * now-focusable preview image instead of on the close control. */
  let close = body.querySelector(".preview-close-btn");
  if (!close) {
    close = document.createElement("button");
    close.type = "button";
    close.className = "preview-close-btn";
    close.dataset.tofuAction = "closePreview()";
    close.setAttribute("aria-label", typeof t === 'function' ? t('common.close') : 'Close');
    close.textContent = "✕";
  }
  const image = document.createElement("img");
  image.src = String(src);
  image.alt = "Preview";
  image.className = "preview-image";
  image.tabIndex = 0;
  image.setAttribute("role", "button");
  image.title = typeof t === 'function' ? t('markdown.previewImage') : 'Enlarge image';
  _previewImageNatural(image);
  image.addEventListener("load", () => {
    if (image.isConnected && !image.classList.contains("preview-image--whole")) {
      _previewImageNatural(image);
    }
  }, { once: true });
  image.addEventListener("click", () => togglePreviewImageZoom(image));
  image.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    event.preventDefault();
    togglePreviewImageZoom(image);
  });
  for (const child of Array.from(body.children)) {
    if (child !== close) child.remove();
  }
  if (close.parentElement !== body) body.prepend(close);
  body.append(image);
  showAttachmentPreview(document, typeof t === 'function' ? t('markdown.previewTitle') : 'Attachment preview');
}
function openTextPreview(title, meta, text) {
  // Last-line-of-defence: an empty / whitespace-only body would render an
  //   empty <pre>, collapsing the flex panel to just its header — the "single
  //   bar" popup bug. ANY caller passing empty text (e.g. an inject row whose
  //   previews resolved to "") must still show a visible, localized note so the
  //   modal never degenerates. Row-agnostic on purpose.
  const _t = (typeof t === "function") ? t : (k, d) => d;
  const body = (text != null && String(text).trim())
    ? escapeHtml(text)
    : `<span class="preview-text-empty">${escapeHtml(_t("tool.noContent", "No content returned."))}</span>`;
  document.getElementById("previewBody").innerHTML =
    `<button class="preview-close-btn" data-tofu-action="closePreview()" aria-label="Close">✕</button><div class="preview-text-panel"><div class="preview-text-header"><span class="preview-text-title">${escapeHtml(title)}</span><span class="preview-text-meta">${escapeHtml(meta)}</span></div><pre class="preview-text-body">${body}</pre></div>`;
  showAttachmentPreview(document, typeof t === 'function' ? t('markdown.previewTitle') : 'Attachment preview');
}
function closePreview() {
  closeAttachmentPreview(document);
}

// Event delegation for ptool-truncated "show all" bars (static render path)
document.addEventListener('click', function(e) {
  const trunc = e.target.closest('.ptool-truncated');
  if (!trunc) return;
  const body = trunc.closest('.ptool-panel-body');
  if (!body) { trunc.remove(); return; }
  // Resolve the owning Turn by its stable contract identity.
  const turnEl = trunc.closest('.message[data-turn-id]');
  if (turnEl) {
    const conv = getActiveConv();
    const state = conv && runtimeScope.ConversationTurnStore
      ?.ensureRuntimeStore?.(conv.id)?.getState?.();
    const turn = state?.turnsById?.[turnEl.dataset.turnId];
    if (turn) {
      const allRounds = turn.projection?.toolRounds || [];
      if (allRounds.length > 0) {
        trunc.remove();
        /* Render the full grouped structure (parallel-batch .ptool-turn
         * containers) in one shot via the shared helper so the expanded
         * view matches the streaming/static layout exactly. */
        if (typeof _renderToolGroupsHTML === 'function') {
          body.innerHTML = _renderToolGroupsHTML(allRounds, allRounds);
        } else {
          body.innerHTML = '';
          for (const round of allRounds) {
            const slot = document.createElement('div');
            slot.setAttribute('data-prn', round.roundNum);
            slot.innerHTML = typeof _renderUnifiedToolLine === 'function'
              ? _renderUnifiedToolLine(round, false)
              : `<div class="ptool-line"><span class="ptool-text">${escapeHtml(round.toolName || round.query || '')}</span></div>`;
            body.appendChild(slot);
          }
        }
        return;
      }
    }
  }
  // Fallback: just remove the truncation bar
  trunc.remove();
});
