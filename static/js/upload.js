/* ═══════════════════════════════════════════
   upload.js — File Upload, Preview & VLM
   ═══════════════════════════════════════════ */
var pendingPdfTexts = [];  // shared with main.js — must be var for cross-script access

// ── VLM sessionStorage persistence ──
// Key: 'tofu_vlm_pending' → JSON array of {name, text, pages, textLength, isScanned, method, vlmStatus, vlmTaskId, vlmProgress}
// Migrate from legacy 'chatui_vlm_pending' once on load so users mid-VLM-batch
// don't lose their pending state when they refresh after the rename rollout.
var _VLM_STORAGE_KEY = 'tofu_vlm_pending';
(function _migrateLegacyVlmKey() {
  try {
    const _legacy = sessionStorage.getItem('chatui_vlm_pending');
    if (_legacy && sessionStorage.getItem(_VLM_STORAGE_KEY) == null) {
      sessionStorage.setItem(_VLM_STORAGE_KEY, _legacy);
      sessionStorage.removeItem('chatui_vlm_pending');
    }
  } catch (_e) { /* sessionStorage may be disabled — no-op */ }
})();

/** Save current pendingPdfTexts + VLM task state to sessionStorage. */
function _vlmSaveState() {
  try {
    var items = pendingPdfTexts.map(function(p) {
      return {
        name: p.name, text: p.text, pages: p.pages,
        textLength: p.textLength, isScanned: p.isScanned,
        method: p.method, vlmStatus: p.vlmStatus || '',
        vlmTaskId: p._vlmTaskId || '', vlmProgress: p.vlmProgress || '',
        _docIcon: p._docIcon || '',
      };
    });
    if (items.length > 0) {
      sessionStorage.setItem(_VLM_STORAGE_KEY, JSON.stringify(items));
    } else {
      sessionStorage.removeItem(_VLM_STORAGE_KEY);
    }
  } catch (e) { /* quota exceeded — ignore */ }
}

/** Clear VLM persistence from sessionStorage. */
function _vlmClearState() {
  try { sessionStorage.removeItem(_VLM_STORAGE_KEY); } catch (e) { /* ignore */ }
}

/**
 * Restore pendingPdfTexts from sessionStorage after page refresh.
 * For entries that were VLM-parsing, attempt to reconnect to the server task.
 * Call this once on page load (before initActiveTasks).
 */
async function _vlmRestoreState() {
  var raw;
  try { raw = sessionStorage.getItem(_VLM_STORAGE_KEY); } catch (e) { return; }
  if (!raw) return;
  _vlmClearState();  // consume once — will re-save as polling progresses
  var items;
  try { items = JSON.parse(raw); } catch (e) { return; }
  if (!Array.isArray(items) || items.length === 0) return;

  console.log('%c[VLM-Restore] Recovering %d PDF(s) from sessionStorage', 'color:#f59e0b;font-weight:bold', items.length);

  for (var i = 0; i < items.length; i++) {
    var saved = items[i];
    var pdfObj = {
      name: saved.name, text: saved.text || '', pages: saved.pages || 0,
      textLength: saved.textLength || 0, isScanned: !!saved.isScanned,
      method: saved.method || 'text',
      vlmStatus: saved.vlmStatus || '', vlmProgress: saved.vlmProgress || '',
      _vlmAlive: true, _docIcon: saved._docIcon || '',
    };
    pendingPdfTexts.push(pdfObj);

    // If VLM was in progress, try to reconnect
    if (saved.vlmStatus === 'parsing' && saved.vlmTaskId) {
      pdfObj._vlmTaskId = saved.vlmTaskId;
      pdfObj.vlmStatus = 'parsing';
      // Resume polling in background
      _vlmResumePoll(pdfObj, saved.vlmTaskId);
    } else if (saved.vlmStatus === 'parsing' && saved.name) {
      // No taskId saved — try to find by filename on server
      _vlmReconnectByFilename(pdfObj, saved.name);
    }
    // For done/failed/timeout/unavailable entries, just restore as-is
  }
  renderImagePreviews();
}

/** Resume VLM polling for a known taskId (after refresh). */
function _vlmResumePoll(entry, taskId) {
  console.log('[VLM-Restore] Resuming poll for task %s (%s)', taskId, entry.name);
  var onUpdate = function() { renderImagePreviews(); _vlmSaveState(); };
  var isAlive = function() { return entry._vlmAlive !== false; };
  // Run the polling part of _vlmParseEntry (no need to re-upload the file)
  _vlmPollTask(entry, taskId, isAlive, onUpdate);
}

/** Try to reconnect to a VLM task by filename when taskId was lost. */
async function _vlmReconnectByFilename(entry, filename) {
  try {
    var data = await Api.pdf.vlmTasks(filename);
    if (!data) {
      console.warn('[VLM-Restore] Task lookup failed for %s', filename);
      entry.vlmStatus = 'unavailable';
      renderImagePreviews();
      return;
    }
    if (!data.tasks || data.tasks.length === 0) {
      console.warn('[VLM-Restore] No active VLM task found for %s', filename);
      // Task may have completed and expired — keep text parse result
      if (entry.text) {
        entry.vlmStatus = '';  // clear stale parsing status
      } else {
        entry.vlmStatus = 'unavailable';
      }
      renderImagePreviews();
      return;
    }
    // Use the most recent task
    var task = data.tasks[0];
    console.log('[VLM-Restore] Found task %s for %s (status=%s)', task.taskId, filename, task.status);
    entry._vlmTaskId = task.taskId;
    if (task.status === 'processing') {
      entry.vlmStatus = 'parsing';
      entry.vlmProgress = task.progress;
      var onUpdate = function() { renderImagePreviews(); _vlmSaveState(); };
      var isAlive = function() { return entry._vlmAlive !== false; };
      _vlmPollTask(entry, task.taskId, isAlive, onUpdate);
    } else if (task.status === 'done') {
      // Fetch full result
      var taskData = await Api.pdf.vlmPoll(task.taskId);
      if (taskData && taskData.result) {
        entry.text = taskData.result;
        entry.textLength = taskData.textLength || taskData.result.length;
        entry.method = 'vlm';
        entry.vlmStatus = 'done';
      }
    } else if (task.status === 'error') {
      entry.vlmStatus = 'failed';
    }
    renderImagePreviews();
    _vlmSaveState();
  } catch (e) {
    console.warn('[VLM-Restore] Reconnect failed for %s:', filename, e);
    entry.vlmStatus = 'unavailable';
    renderImagePreviews();
  }
}



// ── Image/PDF upload ──
async function uploadImageToServer(imgObj) {
  try {
    const data = await Api.images.upload({ base64: imgObj.base64, mediaType: imgObj.mediaType });
    if (data && data.url) {
      imgObj.url = apiUrl(data.url);
    }
  } catch (e) {
    debugLog("Image upload failed: " + e.message, "warn");
  }
}
/**
 * Client-side image pre-shrink. Mirrors the backend's _shrink_upload_image()
 * policy (routes/upload.py) exactly — thresholds come from /api/server-config
 * via window._uploadShrinkPolicy. The client-side pass only exists to cap
 * the UPLOAD wire (phone photos over mobile data) and avoid hitting Flask's
 * MAX_CONTENT_LENGTH; the server always runs the same logic again and owns
 * the final on-disk bytes.
 *
 * @param {File}   file
 * @param {number} userMaxWidth  Optional user override from Settings.
 *                               `0` / falsy  → follow server policy.
 *                               `>0`         → TIGHTEN only (min with server cap).
 */
function compressImage(file, userMaxWidth) {
  // ── Resolve policy from server, with safe fallbacks that match the backend
  //    defaults in routes/upload.py. If the /api/server-config request hasn't
  //    returned yet, these fallbacks keep us consistent instead of guessing. ──
  const policy = (typeof window !== 'undefined' && window._uploadShrinkPolicy) || {};
  const SRV_MAX_PX    = policy.max_long_side_px  || 2048;   // MAX_UPLOAD_LONG_SIDE_PX
  const SRV_QUALITY   = (policy.jpeg_quality     || 90) / 100;
  const SKIP_PX       = policy.skip_long_side_px || 1600;   // SHRINK_SKIP_LONG_SIDE_PX
  const SKIP_BYTES    = policy.skip_max_bytes    || (400 * 1024);

  // User override can only TIGHTEN the cap (smaller = stricter). A 0/negative
  // value means "trust the server policy" — the new default.
  const effectiveMaxPx = (userMaxWidth && userMaxWidth > 0)
    ? Math.min(userMaxWidth, SRV_MAX_PX)
    : SRV_MAX_PX;

  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (ev) => {
      const dataUrl = String(ev.target.result || "");
      const originalBytes = Math.round((dataUrl.length * 3) / 4);
      const passthrough = () => resolve({
        base64: dataUrl.split(",")[1],
        mediaType: file.type,
        preview: dataUrl,
        sizeKB: Math.round(originalBytes / 1024),
      });

      // ── Load image to inspect dimensions ──
      const img = new Image();
      img.onload = () => {
        const { width: origW, height: origH } = img;
        const longSide = Math.max(origW, origH);

        // Skip rule — mirrors backend: small enough in BOTH dims AND bytes → pass through
        // untouched (lossless, identical to what the server would keep).
        if (longSide <= SKIP_PX && originalBytes <= SKIP_BYTES && longSide <= effectiveMaxPx) {
          passthrough();
          return;
        }

        // Resize if over the long-side cap; otherwise keep original dims but still re-encode
        // only when we have to (i.e., we failed the skip rule above, which means size cap
        // was exceeded or dims exceed user override).
        let w = origW, h = origH;
        if (longSide > effectiveMaxPx) {
          const scale = effectiveMaxPx / longSide;
          w = Math.max(1, Math.round(origW * scale));
          h = Math.max(1, Math.round(origH * scale));
        }
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        const t = file.type === "image/png" ? "image/png" : "image/jpeg";
        const d = canvas.toDataURL(t, SRV_QUALITY);

        // Sanity: if re-encode grew the payload (rare — tiny high-entropy JPEGs),
        // keep the original. Same guard as the backend.
        const newBytes = Math.round((d.length * 3) / 4);
        if (newBytes >= originalBytes && w === origW && h === origH) {
          passthrough();
          return;
        }
        resolve({
          base64: d.split(",")[1],
          mediaType: t,
          preview: d,
          sizeKB: Math.round(newBytes / 1024),
        });
      };
      img.onerror = () => {
        // Couldn't decode in browser — just send original, server will re-check.
        passthrough();
      };
      img.src = dataUrl;
    };
    reader.readAsDataURL(file);
  });
}
// ── Shared core: process an image file (compress + upload) ──
// Returns a fully-formed imgObj with base64, mediaType, preview, sizeKB, url.
// Used by: handleFileUpload, paste handler, drag-drop, edit mode.
async function processImageFile(file) {
  // ★ imageMaxWidth is now an OPTIONAL user override that can only TIGHTEN
  // the server's policy (lower = stricter). 0 / unset = follow server.
  // Legacy default of 1024 was the bug behind the "uploaded image is blurry"
  // complaint — we no longer hardcode any cap on the client.
  const d = await compressImage(file, config.imageMaxWidth || 0);
  await uploadImageToServer(d);
  return d;
}

// ── Shared core: parse a PDF file via server backend ──
// Populates and returns a pdfObj. If opts.startVlm is true and an onUpdate
// callback is provided, auto-starts VLM background parse.
// Used by: handlePDFUpload, drag-drop, edit mode.
async function parsePdfToServer(file, pdfObj, opts) {
  const { onUpdate, isAlive } = opts || {};
  const formData = new FormData();
  formData.append("file", file);
  formData.append("maxImageWidth", "0");
  formData.append("maxImages", "0");
  formData.append("maxTextChars", "0");
  const data = await Api.pdf.parse(formData);
  if (!data || !data.success) throw new Error((data && data.error) || "Parse failed");
  pdfObj.text = data.text || "";
  pdfObj.pages = data.totalPages;
  pdfObj.textLength = data.textLength;
  pdfObj.isScanned = data.isScanned;
  pdfObj.method = data.method;
  if (onUpdate) onUpdate();
  _vlmSaveState();  // ★ Persist text parse result so it survives refresh
  // Auto-start VLM high-quality parse in background
  if (typeof window._vlmParseEntry === "function" && onUpdate) {
    pdfObj._vlmAlive = true;
    const alive = isAlive || (() => pdfObj._vlmAlive !== false);
    window._vlmParseEntry(file, pdfObj, alive, onUpdate);
  }
  return { data }; // caller can inspect data.textLength, data.isScanned etc.
}

// ── Document extensions recognized for server-side parsing ──
var _DOC_EXTS = new Set([
  '.docx', '.doc', '.pptx', '.ppt', '.xlsx', '.xls',
  '.txt', '.md', '.markdown', '.csv', '.tsv',
  '.json', '.jsonl', '.xml', '.html', '.htm',
  '.log', '.yaml', '.yml', '.toml', '.ini', '.cfg',
  '.rst', '.tex', '.bib', '.srt', '.vtt',
  '.py', '.js', '.ts', '.java', '.c', '.cpp', '.h', '.hpp',
  '.go', '.rs', '.rb', '.php', '.sh', '.bash', '.zsh',
  '.css', '.scss', '.less', '.sql', '.r', '.m', '.swift',
]);
function _getFileExt(name) {
  const i = name.lastIndexOf('.');
  return i >= 0 ? name.slice(i).toLowerCase() : '';
}

async function handleFileUpload(e) {
  const files = Array.from(e.target.files);
  // 2026-05-06 (Option C): launch ALL uploads in parallel. Previously we
  // serialized via `for...of await` so a slow first PDF blocked everything.
  // Each call already manages its own UI state, and the server is threaded.
  const tasks = files.map(f => {
    if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"))
      return handlePDFUpload(f);
    if (f.type.startsWith("image/"))
      return _handleImageDrop(f);
    if (_DOC_EXTS.has(_getFileExt(f.name)))
      return handleDocUpload(f);
    return Promise.resolve();
  });
  e.target.value = "";  // reset picker immediately so user can queue more
  await Promise.allSettled(tasks);
}

// 2026-05-06 (Option C): PDFs upload in PARALLEL. The legacy single-flight
// `pdfProcessing` mutex serialized N PDFs, so a slow 60-page paper blocked
// every subsequent upload — making the second card look "stuck" while the
// first text-extracted. The Werkzeug server is already threaded=True, and
// each upload runs on its own request thread, so true parallelism is fine.
// `pdfProcessing` is now an integer counter for "any text parse in flight"
// (used by sendMessage's precondition + global progress bar). VLM stage is
// tracked separately via per-entry `vlmStatus`.

/** Returns true while ANY pdf entry is still in the text-extract phase. */
function _isAnyPdfTextParsing() {
  return pendingPdfTexts.some(p => p && p.method === 'parsing');
}

/** Repaint the shared progress bar based on active text-parses. */
function _refreshPdfProgressBar() {
  const pEl = document.getElementById("pdfProgress");
  const pText = document.getElementById("pdfProgressText");
  const pFill = document.getElementById("pdfProgressFill");
  if (!pEl || !pText || !pFill) return;
  const inFlight = pendingPdfTexts.filter(p => p && p.method === 'parsing');
  if (inFlight.length === 0) return;  // hidden by per-call cleanup
  pEl.style.display = "flex";
  if (inFlight.length === 1) {
    pText.textContent = `Extracting text from "${inFlight[0].name}"…`;
  } else {
    const names = inFlight.map(p => p.name.length > 18 ? p.name.slice(0, 16) + '…' : p.name);
    pText.textContent = `Extracting text from ${inFlight.length} PDFs: ${names.join(', ')}`;
  }
  pFill.style.width = "30%";
}

async function handlePDFUpload(file) {
  // Track via global counter so the send-precondition can still ask
  // "any text parses in flight?" without being a single-flight gate.
  if (typeof pdfProcessing !== 'number') pdfProcessing = 0;
  pdfProcessing += 1;
  const pEl = document.getElementById("pdfProgress"),
    pText = document.getElementById("pdfProgressText"),
    pFill = document.getElementById("pdfProgressFill");
  // Optimistic placeholder card — appears IMMEDIATELY so the user sees
  // the second/third PDF acknowledged even while the first is still parsing.
  const pdfObj = { name: file.name, text: "", pages: 0, textLength: 0, isScanned: false, method: "parsing" };
  pendingPdfTexts.push(pdfObj);
  renderImagePreviews();
  _refreshPdfProgressBar();
  try {
    const { data } = await parsePdfToServer(file, pdfObj, {
      onUpdate: () => renderImagePreviews(),
      isAlive: () => pdfObj._vlmAlive !== false,
    });
    debugLog(
      `PDF text: ${file.name} — ${data.textLength.toLocaleString()} chars, ${data.totalPages} pages`,
      "success",
    );
    renderImagePreviews();
    // Only print the per-file success line in the global bar if no other
    // parse is in flight (otherwise the multi-file message takes over).
    if (!_isAnyPdfTextParsing()) {
      const parts = [];
      if (data.textLength > 0)
        parts.push(`${data.textLength.toLocaleString()} chars text`);
      if (data.isScanned) parts.push("scanned");
      parts.push(`method: ${data.method}`);
      if (data.warnings?.length > 0) parts.push(`⚠️ ${data.warnings.join("; ")}`);
      pText.textContent = `✓ ${file.name}: ${data.totalPages} pages — ${parts.join(" · ")}`;
      pFill.style.width = "100%";
      if (data.isScanned && data.textLength < 100)
        pText.textContent = `⚠️ "${file.name}" is a scanned PDF with minimal extractable text.`;
      setTimeout(() => {
        if (!_isAnyPdfTextParsing()) pEl.style.display = "none";
      }, 3000);
    } else {
      _refreshPdfProgressBar();
    }
  } catch (err) {
    console.error(
      "[PDF] Backend parse failed for '%s':",
      file.name, err.message, err,
    );
    // Remove this PDF's placeholder entry on failure
    const failIdx = pendingPdfTexts.indexOf(pdfObj);
    if (failIdx >= 0) pendingPdfTexts.splice(failIdx, 1);
    renderImagePreviews();
    const is413 = err.message && (err.message.includes('413') || err.message.toLowerCase().includes('too large'));
    const reason = is413
      ? 'File too large for server (413)'
      : `Server error: ${err.message}`;
    pFill.style.width = "0%";
    pText.textContent = `⚠️ PDF parse failed for "${file.name}": ${reason}.`;
    console.error("[PDF] Upload failed — reason: %s, file: %s, size: %d bytes", reason, file.name, file.size);
    debugLog(`PDF upload failed: ${file.name} — ${reason}`, "error");
    setTimeout(() => {
      if (!_isAnyPdfTextParsing()) pEl.style.display = "none";
    }, 5000);
  } finally {
    pdfProcessing = Math.max(0, (pdfProcessing | 0) - 1);
  }
}



// ── Shared helper: process an image from drag-drop or file picker ──
async function _handleImageDrop(f) {
  const d = await processImageFile(f);
  pendingImages.push(d);
  renderImagePreviews();
  if (typeof _igUpdateGenButton === 'function') _igUpdateGenButton();
}

// ── Document upload (Word, Excel, PPT, plain text) → server-side parse ──
async function handleDocUpload(file) {
  const pEl = document.getElementById("pdfProgress"),
    pText = document.getElementById("pdfProgressText"),
    pFill = document.getElementById("pdfProgressFill");
  pEl.style.display = "flex";
  pText.textContent = `Parsing "${file.name}"…`;
  pFill.style.width = "10%";

  // Determine icon by extension
  const ext = _getFileExt(file.name);
  const iconMap = {'.docx':'📝', '.pptx':'📊', '.xlsx':'📈', '.txt':'📄', '.md':'📄',
                   '.csv':'📊', '.json':'📄', '.xml':'📄', '.py':'🐍', '.js':'📜',
                   '.html':'🌐', '.yaml':'⚙️', '.yml':'⚙️'};
  const icon = iconMap[ext] || '📄';

  try {
    const formData = new FormData();
    formData.append("file", file);
    formData.append("maxTextChars", "0");
    const data = await Api.doc.parse(formData);
    if (!data || !data.success) throw new Error((data && data.error) || "Parse failed");

    const docObj = {
      name: file.name,
      text: data.text || "",
      pages: data.totalPages || 1,
      textLength: data.textLength || 0,
      isScanned: false,
      method: data.method || ext,
      _docIcon: icon,
    };
    pendingPdfTexts.push(docObj);
    renderImagePreviews();
    _vlmSaveState();  // ★ Persist doc upload for refresh recovery

    pFill.style.width = "100%";
    const sizeStr = data.textLength >= 1024
      ? `${(data.textLength / 1024).toFixed(1)}KB` : `${data.textLength} chars`;
    const parts = [sizeStr];
    if (data.warnings?.length) parts.push(`⚠️ ${data.warnings.join("; ")}`);
    pText.textContent = `✓ ${file.name}: ${parts.join(" · ")}`;
    debugLog(`Doc parsed: ${file.name} — ${sizeStr}, method: ${data.method}`, "success");
    setTimeout(() => { pEl.style.display = "none"; }, 2500);
  } catch (err) {
    console.warn("[Doc] Parse failed:", err.message);
    pText.textContent = `⚠️ Failed to parse "${file.name}": ${err.message}`;
    pFill.style.width = "0%";
    setTimeout(() => { pEl.style.display = "none"; }, 4000);
  }
}

function renderImagePreviews() {
  let html = "";
  html += pendingPdfTexts
    .map((pdf, i) => {
      const sizeStr =
        pdf.textLength >= 1024
          ? `${(pdf.textLength / 1024).toFixed(1)}KB`
          : `${pdf.textLength} chars`;
      const badge = pdf.isScanned ? " (scanned)" : "";
      // VLM status indicator
      const vlmS = pdf.vlmStatus || "";
      const vlmBadge =
        vlmS === "parsing"
          ? `<div class="pdf-vlm-badge parsing">🔄 VLM ${pdf.vlmProgress || "..."}</div>`
          : vlmS === "done"
            ? `<div class="pdf-vlm-badge done">✅ VLM</div>`
            : vlmS === "failed" || vlmS === "timeout"
              ? `<div class="pdf-vlm-badge failed">⚠️ VLM</div>`
              : "";
      const methodLabel = pdf.method === "vlm" ? "VLM" : "TEXT";
      const docIcon = pdf._docIcon || "📄";
      return `<div class="img-preview pdf-text-card" onclick="previewPendingPdfText(${i})"><div class="pdf-text-card-inner"><div class="pdf-text-icon">${docIcon}</div><div class="pdf-text-info"><div class="pdf-text-name" title="${escapeHtml(pdf.name)}">${escapeHtml(pdf.name.length > 20 ? pdf.name.slice(0, 18) + "…" : pdf.name)}</div><div class="pdf-text-meta">${pdf.pages}p · ${sizeStr}${badge}</div>${vlmBadge}</div></div><button class="remove-img" onclick="event.stopPropagation();removePdfText(${i})">✕</button><div class="img-size">${methodLabel}</div></div>`;
    })
    .join("");
  html += pendingImages
    .map((img, i) => {
      const isPdf = !!img.pdfPage;
      const srcMap = {
        clip_render: "CLIP",
        vector_clip: "VEC",
        page_render: "SCAN",
        embedded: "RAW",
        pixmap_fallback: "PIX",
        pymupdf4llm: "FIG",
        figure_page_render: "FIG",
      };
      const srcLabel = srcMap[img.pdfImageSource] || (isPdf ? "PDF" : "");
      const label = isPdf
        ? `P${img.pdfPage}/${img.pdfTotal} · ${img.sizeKB}KB`
        : `${img.sizeKB || "?"}KB`;
      const tip = img.caption
        ? `Page ${img.pdfPage}: ${img.caption}`.replace(/"/g, "&quot;")
        : isPdf
          ? `PDF page ${img.pdfPage}`
          : "";
      return `<div class="img-preview${isPdf ? " pdf-page" : ""}" draggable="true" data-img-idx="${i}" ${tip ? `title="${tip}"` : ""}  onclick="previewPendingImage(${i})"><img src="${img.preview}" alt="preview" draggable="false">${srcLabel ? `<div class="pdf-badge">${srcLabel}</div>` : ""}<button class="remove-img" onclick="event.stopPropagation();removeImage(${i})">✕</button><div class="img-size">${label}</div></div>`;
    })
    .join("");
  // ★ Target-aware: render into edit area when editing, main input otherwise
  const targetId = (typeof _editingMsgIdx !== 'undefined' && _editingMsgIdx !== null)
    ? "editImagePreviews" : "imagePreviews";
  const targetEl = document.getElementById(targetId);
  if (targetEl) targetEl.innerHTML = html;
  // ★ Keep the other container in sync (clear it)
  const otherId = targetId === "editImagePreviews" ? "imagePreviews" : "editImagePreviews";
  const otherEl = document.getElementById(otherId);
  if (otherEl) otherEl.innerHTML = "";
}
function removeImage(i) {
  pendingImages.splice(i, 1);
  renderImagePreviews();
  if (typeof _igUpdateGenButton === 'function') _igUpdateGenButton();
}

// ── Drag-to-reorder image preview chips ──────────────
// Image chips carry draggable="true" + data-img-idx. We move the dragged
// entry within pendingImages on drop. Document-level delegation is used so
// the handlers survive renderImagePreviews()'s innerHTML rebuilds.
var _imgDragFromIdx = null;
function _imgChipFrom(target) {
  const chip = target && target.closest ? target.closest('.img-preview[data-img-idx]') : null;
  if (!chip) return null;
  // Only chips inside an image-previews container are reorderable images
  // (pdf-text-card lacks data-img-idx, so closest already filters those out).
  const idx = parseInt(chip.dataset.imgIdx, 10);
  return Number.isInteger(idx) ? { chip, idx } : null;
}
document.addEventListener('dragstart', (e) => {
  const hit = _imgChipFrom(e.target);
  if (!hit) return;
  _imgDragFromIdx = hit.idx;
  hit.chip.classList.add('img-dragging');
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move';
    // Required for Firefox to initiate the drag.
    try { e.dataTransfer.setData('text/plain', String(hit.idx)); } catch (_e) { /* ignore */ }
  }
});
document.addEventListener('dragend', (e) => {
  const hit = _imgChipFrom(e.target);
  if (hit) hit.chip.classList.remove('img-dragging');
  document.querySelectorAll('.img-preview.img-drop-target')
    .forEach((el) => el.classList.remove('img-drop-target'));
  _imgDragFromIdx = null;
});
document.addEventListener('dragover', (e) => {
  if (_imgDragFromIdx === null) return;
  const hit = _imgChipFrom(e.target);
  if (!hit) return;
  e.preventDefault();  // allow drop
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
  document.querySelectorAll('.img-preview.img-drop-target')
    .forEach((el) => { if (el !== hit.chip) el.classList.remove('img-drop-target'); });
  if (hit.idx !== _imgDragFromIdx) hit.chip.classList.add('img-drop-target');
});
document.addEventListener('drop', (e) => {
  if (_imgDragFromIdx === null) return;
  const hit = _imgChipFrom(e.target);
  if (!hit) return;
  e.preventDefault();
  e.stopPropagation();
  const from = _imgDragFromIdx;
  const to = hit.idx;
  _imgDragFromIdx = null;
  if (from === to || from < 0 || from >= pendingImages.length) { renderImagePreviews(); return; }
  const moved = pendingImages.splice(from, 1)[0];
  pendingImages.splice(to, 0, moved);
  renderImagePreviews();
}, true);  // capture: run before the full-page file-drop handler
function removePdfText(i) {
  const entry = pendingPdfTexts[i];
  if (entry) entry._vlmAlive = false; // ★ Kill VLM polling for this entry
  pendingPdfTexts.splice(i, 1);
  renderImagePreviews();
  _vlmSaveState();  // ★ Update persistence
}

// ── VLM PDF async parse (shared core) ────────────────

/**
 * Shared VLM polling loop — used by both fresh parse and refresh-resume.
 * @param entry    - pdf entry object to mutate
 * @param taskId   - server task ID
 * @param isAlive  - () => boolean
 * @param onUpdate - () => void
 */
async function _vlmPollTask(entry, taskId, isAlive, onUpdate) {
  for (let i = 0; i < 150; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    if (!isAlive()) { _vlmSaveState(); return; }
    try {
      const task = await Api.pdf.vlmPoll(taskId);
      if (!task) break;
      if (task.status === "processing") {
        entry.vlmStatus = "parsing";
        entry.vlmProgress = task.progress;
        onUpdate();
        continue;
      }
      if (task.status === "done" && task.result) {
        // Quality gate: count pipe-tables in old vs new text
        const countTables = (s) => (s.match(/^\|.+\|$/gm) || []).length;
        const oldTables = countTables(entry.text || "");
        const newTables = countTables(task.result);
        if (oldTables > 2 && newTables === 0) {
          console.warn(
            `[VLM] ${entry.name}: VLM result dropped ${oldTables} table rows → keeping original`,
          );
          entry.vlmStatus = "done-skipped";
          onUpdate();
          debugLog(
            `[VLM] ${entry.name}: VLM dropped tables (${oldTables}→${newTables}), kept original`,
            "warn",
          );
          return;
        }
        entry.text = task.result;
        entry.textLength = task.textLength || task.result.length;
        entry.method = "vlm";
        entry.vlmStatus = "done";
        onUpdate();
        debugLog(
          `[VLM] ${entry.name}: upgraded to VLM parse, ${entry.textLength} chars`,
        );
        return;
      }
      if (task.status === "error") {
        console.warn("[VLM] parse error:", task.error);
        entry.vlmStatus = "failed";
        onUpdate();
        return;
      }
    } catch (pollErr) {
      console.warn("[VLM] poll error:", pollErr);
    }
  }
  // timeout
  entry.vlmStatus = "timeout";
  onUpdate();
}

// Generic VLM parse: works for both main input and edit mode.
// @param file      - File object
// @param entry     - pdf entry object to mutate (vlmStatus, text, etc.)
// @param isAlive   - () => boolean, returns false if entry was removed/cancelled
// @param onUpdate  - () => void, called after each entry mutation to refresh UI
window._vlmParseEntry = async function(file, entry, isAlive, onUpdate) {
  if (!entry) return;
  entry.vlmStatus = "parsing";
  onUpdate();
  _vlmSaveState();
  try {
    const fd = new FormData();
    fd.append("file", file);
    let startData;
    try {
      startData = await Api.pdf.vlmStart(fd);
    } catch (startErr) {
      console.warn("[VLM] start failed:", startErr.message);
      entry.vlmStatus = "unavailable";
      onUpdate();
      _vlmSaveState();
      return;
    }
    const { taskId } = (startData || {});
    if (!taskId) {
      entry.vlmStatus = "unavailable";
      onUpdate();
      _vlmSaveState();
      return;
    }
    // ★ Persist taskId so we can reconnect after page refresh
    entry._vlmTaskId = taskId;
    _vlmSaveState();
    // Poll for result using shared loop
    await _vlmPollTask(entry, taskId, isAlive, onUpdate);
    _vlmSaveState();
  } catch (err) {
    console.warn("[VLM] error:", err);
    if (isAlive()) {
      entry.vlmStatus = "unavailable";
      onUpdate();
      _vlmSaveState();
    }
  }
};
// _startVlmParse is no longer needed — VLM auto-starts inside parsePdfToServer().

// ══════════════════════════════════════════════════════
//  ★ Preview functions
// ══════════════════════════════════════════════════════
function previewPendingImage(i) {
  const img = pendingImages[i];
  if (!img || !img.preview) return;
  openImagePreview(img.preview);
}
function previewPendingPdfText(i) {
  const pdf = pendingPdfTexts[i];
  if (!pdf) return;
  const sizeStr =
    pdf.textLength >= 1024
      ? `${(pdf.textLength / 1024).toFixed(1)}KB`
      : `${pdf.textLength} chars`;
  openTextPreview(
    `📄 ${pdf.name}`,
    `${pdf.pages} pages · ${sizeStr}`,
    pdf.text || "",
  );
}
function previewMsgPdfText(msgIdx, pdfIdx) {
  const conv = getActiveConv();
  if (!conv) return;
  const msg = conv.messages[msgIdx];
  if (!msg || !msg.pdfTexts || !msg.pdfTexts[pdfIdx]) return;
  const pdf = msg.pdfTexts[pdfIdx];
  const text =
    pdf.text || "(Text not available — content was truncated for storage)";
  const sizeStr =
    (pdf.textLength || 0) >= 1024
      ? `${((pdf.textLength || 0) / 1024).toFixed(1)}KB`
      : `${pdf.textLength || 0} chars`;
  openTextPreview(
    `📄 ${pdf.name}`,
    `${pdf.pages || "?"} pages · ${sizeStr}`,
    text,
  );
}
function openImagePreview(src) {
  if (!src) return;
  document.getElementById("previewBody").innerHTML =
    `<button class="preview-close-btn" onclick="closePreview()" aria-label="Close">✕</button><img src="${src}" alt="Preview" class="preview-image">`;
  document.getElementById("previewModal").classList.add("open");
}
function openTextPreview(title, meta, text) {
  document.getElementById("previewBody").innerHTML =
    `<button class="preview-close-btn" onclick="closePreview()" aria-label="Close">✕</button><div class="preview-text-panel"><div class="preview-text-header"><span class="preview-text-title">${escapeHtml(title)}</span><span class="preview-text-meta">${escapeHtml(meta)}</span></div><pre class="preview-text-body">${escapeHtml(text)}</pre></div>`;
  document.getElementById("previewModal").classList.add("open");
}
function closePreview() {
  document.getElementById("previewModal").classList.remove("open");
  setTimeout(() => {
    document.getElementById("previewBody").innerHTML = "";
  }, 300);
}

// ── Tool Content Preview (search / fetch / project tools) ──
function previewToolContent(roundNum, toolCallId) {
  const conv = getActiveConv();
  if (!conv) return;
  // Search all assistant messages (not just last) to find the round
  for (let i = conv.messages.length - 1; i >= 0; i--) {
    const msg = conv.messages[i];
    if (msg.role !== 'assistant' && msg.role !== 'optimizer') continue;
    const rounds = msg.toolRounds || [];
    const round = rounds.find(r => r.roundNum === roundNum && (toolCallId ? r.toolCallId === toolCallId : true));
    if (round && round.toolContent) {
      const td = typeof _getToolDisplay === 'function' ? _getToolDisplay(round) : { icon: '📄', label: 'Tool' };
      const title = `${td.icon} ${td.label}: ${(round.query || '').slice(0, 80)}`;
      const chars = round.toolContent.length;
      const meta = chars >= 1024 ? `${(chars / 1024).toFixed(1)}KB` : `${chars} chars`;
      openTextPreview(title, meta, round.toolContent);
      return;
    }
  }
}

// Event delegation for tool content preview buttons
document.addEventListener('click', function(e) {
  const btn = e.target.closest('[data-tc-preview]');
  if (!btn) return;
  e.stopPropagation();
  e.preventDefault();
  const rn = parseInt(btn.dataset.tcRn, 10);
  const tcid = btn.dataset.tcTcid || null;
  previewToolContent(rn, tcid);
});

// Event delegation for ptool-truncated "show all" bars (static render path)
document.addEventListener('click', function(e) {
  const trunc = e.target.closest('.ptool-truncated');
  if (!trunc) return;
  const body = trunc.closest('.ptool-panel-body');
  if (!body) { trunc.remove(); return; }
  // Find the message element and its conversation + message data to re-render all rounds
  const msgEl = trunc.closest('.message');
  if (msgEl) {
    const msgIdx = parseInt((msgEl.id || '').replace('msg-', ''), 10);
    const conv = conversations.find(c => c.id === activeConvId);
    if (conv && conv.messages && conv.messages[msgIdx]) {
      const msg = conv.messages[msgIdx];
      const allRounds = getToolRoundsFromMsg(msg);
      if (allRounds.length > 0) {
        trunc.remove();
        body.innerHTML = '';
        /* Render in rAF-chunked batches: building 100+ tool rows synchronously
         * freezes the main thread for ~1s (poor INP). Spreading the work across
         * frames keeps the click responsive — same end state, rows stream in. */
        const _renderSlot = (round) => {
          const slot = document.createElement('div');
          slot.setAttribute('data-prn', round.roundNum);
          slot.innerHTML = typeof _renderUnifiedToolLine === 'function'
            ? _renderUnifiedToolLine(round, false)
            : `<div class="ptool-line"><span class="ptool-text">${escapeHtml(round.toolName || round.query || '')}</span></div>`;
          return slot;
        };
        const CHUNK = 30;
        let _i = 0;
        const _renderChunk = () => {
          const frag = document.createDocumentFragment();
          const end = Math.min(_i + CHUNK, allRounds.length);
          for (; _i < end; _i++) frag.appendChild(_renderSlot(allRounds[_i]));
          body.appendChild(frag);
          if (_i < allRounds.length) requestAnimationFrame(_renderChunk);
        };
        _renderChunk();
        return;
      }
    }
  }
  // Fallback: just remove the truncation bar
  trunc.remove();
});
