/* ═══════════════════════════════════════════
   upload.js — File Upload, Preview & VLM
   ═══════════════════════════════════════════ */
var pendingPdfTexts = [];  // shared with main.js — must be var for cross-script access
var pendingVideos = [];    // video attachments (analysis pipeline) — same window-scope rule

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
      // Store the CANONICAL server-relative URL ('/api/images/<f>'), NOT
      // apiUrl(data.url) — apiUrl() bakes in BASE_PATH (under a reverse proxy
      // that is '/proxy/<port>'), which was persisted into the DB and later
      // broke inspect_image's ref resolution (it expected a bare /api/images/
      // path). Prefixing for display/fetch is done at the consumer via
      // apiUrl(); the stored value stays environment-independent (§3.5).
      imgObj.url = data.url;
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

// Document MIME types (mirrors the `application/*` half of #fileInput's accept).
// Needed because Android content:// URIs handed to <input type=file> often
// carry a display name with NO extension (or a placeholder like "document"),
// so extension-only routing silently drops the file. The ContentResolver
// almost always supplies a MIME type though, so we classify on that too.
var _DOC_MIMES = new Set([
  'application/json', 'application/xml', 'application/x-yaml', 'application/yaml',
  'application/rtf', 'application/msword',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  'application/vnd.ms-excel',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  'application/vnd.ms-powerpoint',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation',
]);
/**
 * True when a picked file should go through the server-side document parser.
 * Extension first (fast, precise), then MIME fallback for extensionless
 * content:// files. `text/*` covers .txt/.md/.csv/.html/source code whose
 * name may lack a suffix on some Android pickers.
 */
function _looksLikeDoc(f) {
  if (_DOC_EXTS.has(_getFileExt(f.name))) return true;
  const mt = (f.type || '').toLowerCase();
  if (!mt) return false;
  if (mt.startsWith('text/')) return true;
  return _DOC_MIMES.has(mt);
}

// MIME → canonical extension. The SERVER's doc parser (lib/doc_parser: both
// is_supported_document and extract_document_text) dispatches PURELY on the
// filename extension — it has NO magic-byte fallback like the PDF route does.
// So an extensionless content:// file (common on Android pickers) would sail
// past the client _looksLikeDoc reroute only to be 400'd server-side. We fix
// that here rather than in the server: synthesize a supported extension on the
// upload filename from the MIME type. `text/*` subtypes we can't map default
// to .txt (the plaintext extractor handles any UTF-8 payload).
var _MIME_TO_EXT = {
  'application/json': '.json', 'application/xml': '.xml',
  'application/x-yaml': '.yaml', 'application/yaml': '.yaml',
  'application/rtf': '.txt', 'application/msword': '.doc',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
  'application/vnd.ms-excel': '.xls',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': '.xlsx',
  'application/vnd.ms-powerpoint': '.ppt',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': '.pptx',
  'text/html': '.html', 'text/csv': '.csv', 'text/markdown': '.md',
  'text/xml': '.xml', 'text/plain': '.txt',
};
/**
 * Filename to send to the server so its extension-based dispatch works.
 * If the picked file already has a supported extension, keep the name as-is;
 * otherwise append an extension derived from the MIME type.
 * @returns {string} a filename ending in a server-supported extension.
 */
function _uploadDocFilename(f) {
  const name = f.name || 'document';
  if (_DOC_EXTS.has(_getFileExt(name))) return name;
  const mt = (f.type || '').toLowerCase();
  let ext = _MIME_TO_EXT[mt];
  if (!ext && mt.startsWith('text/')) ext = '.txt';
  if (!ext) ext = '.txt';  // reached only when _looksLikeDoc already matched
  return name + ext;
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
    if (_looksLikeVideo(f))
      return _handleVideoDrop(f);
    if (_looksLikeDoc(f))
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



// ── Shared helper: process an image from drag-drop, paste or file picker ──
// Optimistic: push a preview chip IMMEDIATELY (via an object URL) so the image
// appears the instant it is selected, then compress + upload in the background
// and reconcile the entry in place. The entry carries `_status:'processing'`
// until both stages finish — sendMessage's _waitForImageProcessing() gate
// blocks on this flag so a still-decoding 2nd/3rd image is never dropped.
async function _handleImageDrop(f) {
  const imgObj = {
    base64: '', mediaType: f.type || 'image/png',
    preview: '', sizeKB: 0, _status: 'processing',
  };
  try {
    imgObj._objectUrl = URL.createObjectURL(f);
    imgObj.preview = imgObj._objectUrl;
  } catch (e) {
    debugLog('createObjectURL failed: ' + e.message, 'warn');
  }
  pendingImages.push(imgObj);
  renderImagePreviews();
  if (typeof _igUpdateGenButton === 'function') _igUpdateGenButton();
  await _processPendingImage(f, imgObj);
}

// Compress + upload an already-previewed image entry, mutating it in place.
async function _processPendingImage(f, imgObj) {
  try {
    const d = await compressImage(f, config.imageMaxWidth || 0);
    if (!pendingImages.includes(imgObj)) return;  // chip removed mid-flight
    imgObj.base64 = d.base64;
    imgObj.mediaType = d.mediaType;
    imgObj.sizeKB = d.sizeKB;
    if (imgObj._objectUrl) {
      try { URL.revokeObjectURL(imgObj._objectUrl); } catch (e) { /* ignore */ }
    }
    imgObj.preview = d.preview;   // canonical data URL (survives reload)
    renderImagePreviews();
    await uploadImageToServer(imgObj);   // sets imgObj.url on success
  } catch (e) {
    debugLog('Image processing failed: ' + e.message, 'warn');
  } finally {
    if (pendingImages.includes(imgObj)) {
      delete imgObj._status;
      delete imgObj._objectUrl;
      renderImagePreviews();
    }
  }
}

// ── Video upload + analysis (P1: frames + transcript) ─────────────
// The video is processed ENTIRELY at upload time (owner ruling 2026-08-04):
// POST → video_id → poll /api/v1/videos/<id> until the server has extracted
// durable frames + transcript. The ready entry's full payload is embedded in
// the conversation message at send time (self-contained, like images[]), so
// reload / resume / multi-turn all keep working.
var _VIDEO_EXTS = new Set(['.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi']);
var _VIDEO_MAX_BYTES = 512 * 1024 * 1024;  // mirrors TOFU_VIDEO_MAX_BYTES default

function _looksLikeVideo(f) {
  const mt = (f.type || '').toLowerCase();
  if (mt.startsWith('video/')) return true;
  // Some pickers (notably .mkv) hand an empty/odd MIME — extension fallback.
  return _VIDEO_EXTS.has(_getFileExt(f.name || ''));
}

function _fmtVideoDur(s) {
  s = Math.max(0, Math.round(s || 0));
  const m = Math.floor(s / 60), ss = s % 60;
  return (m < 10 ? '0' + m : m) + ':' + (ss < 10 ? '0' + ss : ss);
}

async function _handleVideoDrop(f) {
  if (f.size > _VIDEO_MAX_BYTES) {
    debugLog(t('upload.videoTooLarge') + ' — ' + (f.name || ''), 'error');
    return;
  }
  const vObj = { name: f.name || 'video', sizeKB: Math.round((f.size || 0) / 1024), _status: 'uploading' };
  pendingVideos.push(vObj);
  renderImagePreviews();
  try {
    const fd = new FormData();
    fd.append('file', f);
    const up = await Api.videos.upload(fd);
    if (!up || up.ok === false || !up.video_id)
      throw new Error((up && up.error) || 'upload failed');
    vObj.video_id = up.video_id;
    vObj._status = 'processing';
    renderImagePreviews();
    await _pollVideoReady(vObj);
  } catch (e) {
    if (!pendingVideos.includes(vObj)) return;  // chip removed mid-flight
    vObj._status = 'failed';
    vObj._error = String((e && e.message) || e);
    renderImagePreviews();
    debugLog(t('upload.videoFailed') + ': ' + vObj.name + ' — ' + vObj._error, 'error');
  }
}

async function _pollVideoReady(vObj) {
  for (let i = 0; i < 180; i++) {  // 180 × 4s = 12min ceiling (15-min video + transcript)
    await new Promise(r => setTimeout(r, 4000));
    if (!pendingVideos.includes(vObj)) throw new Error('removed');
    let rec = null;
    try { rec = await Api.videos.status(vObj.video_id); } catch (_e) { rec = null; }
    if (!rec) continue;  // transient network — keep polling
    if (rec.status === 'ready') {
      vObj.video_url = rec.video_url || '';
      vObj.poster = rec.poster || '';
      vObj.duration_s = rec.duration_s || 0;
      vObj.width = rec.width || 0;
      vObj.height = rec.height || 0;
      vObj.frames = rec.frames || [];
      vObj.frame_count = rec.frame_count || vObj.frames.length;
      vObj.avg_frame_bytes = rec.avg_frame_bytes || 0;
      vObj.transcript = rec.transcript || '';
      vObj.transcript_status = rec.transcript_status || 'none';
      vObj.transcript_model = rec.transcript_model || '';
      vObj.storyboard = rec.storyboard || '';
      vObj.storyboard_model = rec.storyboard_model || '';
      delete vObj._status;
      delete vObj._phase;
      renderImagePreviews();
      debugLog('Video ready: ' + vObj.name + ' — ' + vObj.frame_count + ' frames, transcript=' + vObj.transcript_status, 'success');
      return;
    }
    if (rec.status === 'failed') throw new Error(rec.error || 'analysis failed');
    if (rec.phase && rec.phase !== vObj._phase) {
      vObj._phase = rec.phase;  // probe → persist → frames → audio
      renderImagePreviews();
    }
  }
  throw new Error('timeout');
}

function removeVideo(i) {
  pendingVideos.splice(i, 1);
  renderImagePreviews();
}

function openVideoUrl(url) {
  if (!url) return;
  window.open(apiUrl(url), '_blank', 'noopener');
}

// The payload embedded into the conversation message at send time — runtime
// fields (_status/_phase/_error) stripped, everything needed for durability
// + server-side model-aware frame clamping kept.
function _videoPayloadForSend(v) {
  return {
    video_id: v.video_id || '',
    name: v.name || 'video',
    video_url: v.video_url || '',
    poster: v.poster || '',
    duration_s: v.duration_s || 0,
    width: v.width || 0,
    height: v.height || 0,
    frames: v.frames || [],
    frame_count: v.frame_count || (v.frames || []).length,
    avg_frame_bytes: v.avg_frame_bytes || 0,
    transcript: v.transcript || '',
    transcript_status: v.transcript_status || 'none',
    transcript_model: v.transcript_model || '',
    storyboard: v.storyboard || '',
    storyboard_model: v.storyboard_model || '',
  };
}

// Send gate: wait for in-flight analyses, then drop any entry that is not
// ready so a half-processed video never rides the turn.
async function _waitForPendingVideos() {
  const deadline = Date.now() + 12 * 60 * 1000;
  while (pendingVideos.some(v => v && (v._status === 'uploading' || v._status === 'processing'))) {
    if (Date.now() > deadline) break;
    await new Promise(r => setTimeout(r, 1000));
  }
  const before = pendingVideos.length;
  pendingVideos = pendingVideos.filter(v => v && !v._status);
  if (pendingVideos.length < before) {
    const msg = t('upload.videoSkipped');
    if (typeof showToast === 'function') showToast(msg, 'warning');
    else debugLog(msg, 'warn');
    renderImagePreviews();
  }
}

// ── Document upload (Word, Excel, PPT, plain text) → server-side parse ──
async function handleDocUpload(file) {
  const pEl = document.getElementById("pdfProgress"),
    pText = document.getElementById("pdfProgressText"),
    pFill = document.getElementById("pdfProgressFill");
  pEl.style.display = "flex";
  pText.textContent = `Parsing "${file.name}"…`;
  pFill.style.width = "10%";

  // The server dispatches on the filename extension only, so ensure the
  // uploaded part carries a supported one (Android content:// names may lack
  // an extension entirely). Icons follow the effective extension too.
  const uploadName = _uploadDocFilename(file);
  const ext = _getFileExt(uploadName);
  const iconMap = {'.docx':Icon('file',22), '.pptx':Icon('slides',22), '.xlsx':Icon('fileSheet',22), '.txt':Icon('file',22), '.md':Icon('file',22),
                   '.csv':Icon('fileSheet',22), '.json':Icon('fileCode',22), '.xml':Icon('fileCode',22), '.py':Icon('fileCode',22), '.js':Icon('fileCode',22),
                   '.html':Icon('fileCode',22), '.yaml':Icon('cog',22), '.yml':Icon('cog',22)};
  const icon = iconMap[ext] || Icon('file',22);

  try {
    const formData = new FormData();
    formData.append("file", file, uploadName);
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
          ? `<div class="pdf-vlm-badge parsing">${Icon('refresh',11)} VLM ${pdf.vlmProgress || "..."}</div>`
          : vlmS === "done"
            ? `<div class="pdf-vlm-badge done">${Icon('file',11)} VLM</div>`
            : vlmS === "failed" || vlmS === "timeout"
              ? `<div class="pdf-vlm-badge failed">${Icon('zap',11)} VLM</div>`
              : "";
      const methodLabel = pdf.method === "vlm" ? "VLM" : "TEXT";
      const docIcon = pdf._docIcon || Icon('file',22);
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
      const processing = img._status === 'processing';
      const overlay = processing
        ? `<div class="img-processing-overlay"><div class="img-processing-spinner"></div></div>`
        : "";
      const dragAttr = processing ? "" : ' draggable="true"';
      return `<div class="img-preview${isPdf ? " pdf-page" : ""}${processing ? " img-processing" : ""}"${dragAttr} data-img-idx="${i}" ${tip ? `title="${tip}"` : ""}  onclick="previewPendingImage(${i})"><img src="${img.preview}" alt="preview" draggable="false">${overlay}${srcLabel ? `<div class="pdf-badge">${srcLabel}</div>` : ""}<button class="remove-img" onclick="event.stopPropagation();removeImage(${i})">✕</button><div class="img-size">${processing ? t('upload.processing') || '…' : label}</div></div>`;
    })
    .join("");
  html += pendingVideos
    .map((v, i) => {
      const busy = v._status === 'uploading' || v._status === 'processing';
      const failed = v._status === 'failed';
      const statusLabel = v._status === 'uploading'
        ? t('upload.videoUploading')
        : v._status === 'processing'
          ? t('upload.videoProcessing') + (v._phase ? ' · ' + v._phase : '')
          : failed ? t('upload.videoFailed') : '';
      const meta = failed ? (v._error || statusLabel)
        : busy ? statusLabel
        : (_fmtVideoDur(v.duration_s) + ' · ' + (v.frame_count || 0) + ' ' + t('upload.videoFrames')
           + (v.transcript ? ' · ' + t('upload.videoTranscript') : ''));
      const thumb = v.poster
        ? `<img src="${apiUrl(v.poster)}" alt="video" draggable="false">`
        : '';
      const click = (!busy && !failed && v.video_url)
        ? ` onclick="openVideoUrl('${String(v.video_url).replace(/'/g, "\\'")}')"` : '';
      const overlay = busy
        ? `<div class="img-processing-overlay"><div class="img-processing-spinner"></div></div>` : '';
      return `<div class="img-preview video-chip${failed ? ' video-failed' : ''}"${click} title="${escapeHtml(v.name)}">${thumb}${overlay}<div class="pdf-badge">VIDEO</div><button class="remove-img" onclick="event.stopPropagation();removeVideo(${i})">✕</button><div class="img-size">${escapeHtml(meta)}</div></div>`;
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
  const gone = pendingImages.splice(i, 1)[0];
  if (gone && gone._objectUrl) {
    try { URL.revokeObjectURL(gone._objectUrl); } catch (e) { /* ignore */ }
  }
  renderImagePreviews();
  if (typeof _igUpdateGenButton === 'function') _igUpdateGenButton();
}

// ── Drag-to-reorder image preview chips ──────────────
// Image chips carry draggable="true" + data-img-idx. We move the dragged
// entry within pendingImages on drop. Document-level delegation is used so
// the handlers survive renderImagePreviews()'s innerHTML rebuilds.
var _imgDragFromIdx = null;
// Floating dashed-square indicator that trails the cursor during a reorder
// drag (desktop mouse), positioned just below-right of the pointer like a
// subscript. Created on dragstart, moved on dragover, removed on dragend/drop.
var _imgDragGhost = null;
function _imgDragGhostShow() {
  if (_imgDragGhost) return;
  _imgDragGhost = document.createElement('div');
  _imgDragGhost.className = 'img-drag-ghost';
  document.body.appendChild(_imgDragGhost);
}
function _imgDragGhostMove(x, y) {
  if (!_imgDragGhost || (!x && !y)) return;  // (0,0) fires on drag-end in some browsers
  _imgDragGhost.style.left = (x + 14) + 'px';
  _imgDragGhost.style.top = (y + 14) + 'px';
}
function _imgDragGhostHide() {
  if (_imgDragGhost) {
    try { _imgDragGhost.remove(); } catch (_e) { /* ignore */ }
    _imgDragGhost = null;
  }
}
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
  _imgDragGhostShow();
  _imgDragGhostMove(e.clientX, e.clientY);
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
  _imgDragGhostHide();
  _imgDragFromIdx = null;
});
document.addEventListener('dragover', (e) => {
  if (_imgDragFromIdx === null) return;
  // A reorder drag is in flight → accept it EVERYWHERE, not only when the
  // pointer is over a sibling chip. dropEffect='move' is what makes the OS
  // show the reposition ("move") cursor; if dragover goes unhandled over the
  // gaps between chips or the surrounding input area, the browser falls back
  // to the no-drop/copy cursor and the gesture feels like a file upload
  // instead of a reposition. Highlight still tracks only the chip under it.
  e.preventDefault();  // allow drop → keeps the move cursor across the whole drag
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
  _imgDragGhostMove(e.clientX, e.clientY);
  const hit = _imgChipFrom(e.target);
  document.querySelectorAll('.img-preview.img-drop-target')
    .forEach((el) => { if (!hit || el !== hit.chip) el.classList.remove('img-drop-target'); });
  if (hit && hit.idx !== _imgDragFromIdx) hit.chip.classList.add('img-drop-target');
});
document.addEventListener('drop', (e) => {
  if (_imgDragFromIdx === null) return;
  // A reorder drag is in flight. Because dragover now accepts the drop
  // EVERYWHERE (to keep the move cursor), we MUST also swallow the drop
  // everywhere — otherwise releasing over the #userInput textarea (or any
  // gap) runs the browser's native text-drop default and inserts our
  // text/plain index payload straight into the input box. Reordering must
  // NEVER mutate anything but the chip order.
  e.preventDefault();
  e.stopPropagation();
  _imgDragGhostHide();
  const from = _imgDragFromIdx;
  _imgDragFromIdx = null;
  const hit = _imgChipFrom(e.target);
  // Off-chip release → no move, just repaint (drop already swallowed above).
  if (!hit || from < 0 || from >= pendingImages.length || hit.idx === from) {
    renderImagePreviews();
    return;
  }
  const moved = pendingImages.splice(from, 1)[0];
  pendingImages.splice(hit.idx, 0, moved);
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