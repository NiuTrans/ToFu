/* ═══════════════════════════════════════════
   upload.js — File Upload, Preview & VLM
   ═══════════════════════════════════════════ */
var pendingPdfTexts = [];  // shared with main.js — must be var for cross-script access



// ── Image/PDF upload ──
async function uploadImageToServer(imgObj) {
  try {
    const resp = await fetch(apiUrl("/api/images/upload"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base64: imgObj.base64,
        mediaType: imgObj.mediaType,
      }),
    });
    if (resp.ok) {
      const data = await resp.json();
      if (data.url) {
        imgObj.url = apiUrl(data.url);
      }
    }
  } catch (e) {
    debugLog("Image upload failed: " + e.message, "warn");
  }
}
function compressImage(file, maxWidth) {
  return new Promise((resolve) => {
    const reader = new FileReader();
    reader.onload = (ev) => {
      if (!maxWidth || maxWidth <= 0) {
        const b = ev.target.result;
        resolve({
          base64: b.split(",")[1],
          mediaType: file.type,
          preview: b,
          sizeKB: Math.round((b.length * 3) / 4 / 1024),
        });
        return;
      }
      const img = new Image();
      img.onload = () => {
        let { width: w, height: h } = img;
        if (w > maxWidth) {
          h = Math.round((h * maxWidth) / w);
          w = maxWidth;
        }
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = h;
        canvas.getContext("2d").drawImage(img, 0, 0, w, h);
        const t = file.type === "image/png" ? "image/png" : "image/jpeg";
        const d = canvas.toDataURL(t, 0.85);
        resolve({
          base64: d.split(",")[1],
          mediaType: t,
          preview: d,
          sizeKB: Math.round((d.length * 3) / 4 / 1024),
        });
      };
      img.src = ev.target.result;
    };
    reader.readAsDataURL(file);
  });
}
// ── Shared core: process an image file (compress + upload) ──
// Returns a fully-formed imgObj with base64, mediaType, preview, sizeKB, url.
// Used by: handleFileUpload, paste handler, drag-drop, edit mode.
async function processImageFile(file) {
  const d = await compressImage(file, config.imageMaxWidth || 1024);
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
  const resp = await fetch(apiUrl("/api/pdf/parse"), { method: "POST", body: formData });
  if (!resp.ok) throw new Error(`Server ${resp.status}`);
  const data = await resp.json();
  if (!data.success) throw new Error(data.error || "Parse failed");
  pdfObj.text = data.text || "";
  pdfObj.pages = data.totalPages;
  pdfObj.textLength = data.textLength;
  pdfObj.isScanned = data.isScanned;
  pdfObj.method = data.method;
  if (onUpdate) onUpdate();
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
  for (const f of files) {
    if (f.type === "application/pdf" || f.name.toLowerCase().endsWith(".pdf"))
      await handlePDFUpload(f);
    else if (f.type.startsWith("image/"))
      await _handleImageDrop(f);
    else if (_DOC_EXTS.has(_getFileExt(f.name)))
      await handleDocUpload(f);
  }
  e.target.value = "";
}

async function handlePDFUpload(file) {
  if (pdfProcessing) return;
  pdfProcessing = true;
  const pEl = document.getElementById("pdfProgress"),
    pText = document.getElementById("pdfProgressText"),
    pFill = document.getElementById("pdfProgressFill");
  pEl.style.display = "flex";
  pText.textContent = `Uploading "${file.name}" for text extraction...`;
  pFill.style.width = "5%";
  try {
    const pdfObj = { name: file.name, text: "", pages: 0, textLength: 0, isScanned: false, method: "parsing" };
    pendingPdfTexts.push(pdfObj);
    const { data } = await parsePdfToServer(file, pdfObj, {
      onUpdate: () => renderImagePreviews(),
      isAlive: () => pdfObj._vlmAlive !== false,
    });
    pFill.style.width = "80%";
    debugLog(
      `PDF text: ${file.name} — ${data.textLength.toLocaleString()} chars, ${data.totalPages} pages`,
      "success",
    );
    renderImagePreviews();
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
      pEl.style.display = "none";
    }, 3000);
  } catch (err) {
    console.error(
      "[PDF] Backend parse failed for '%s':",
      file.name, err.message, err,
    );
    // Remove the placeholder entry on failure
    const failIdx = pendingPdfTexts.findIndex(p => p.name === file.name && p.method === "parsing");
    if (failIdx >= 0) pendingPdfTexts.splice(failIdx, 1);
    renderImagePreviews();
    const is413 = err.message && (err.message.includes('413') || err.message.toLowerCase().includes('too large'));
    const reason = is413
      ? 'File too large for server (413)'
      : `Server error: ${err.message}`;
    pFill.style.width = "0%";
    pText.textContent = `⚠️ PDF parse failed: ${reason}. Please check the server and try again.`;
    console.error("[PDF] Upload failed — reason: %s, file: %s, size: %d bytes", reason, file.name, file.size);
    debugLog(`PDF upload failed: ${file.name} — ${reason}`, "error");
    setTimeout(() => {
      pEl.style.display = "none";
    }, 5000);
  } finally {
    pdfProcessing = false;
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
    const resp = await fetch(apiUrl("/api/doc/parse"), { method: "POST", body: formData });
    if (!resp.ok) throw new Error(`Server ${resp.status}`);
    const data = await resp.json();
    if (!data.success) throw new Error(data.error || "Parse failed");

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
      return `<div class="img-preview${isPdf ? " pdf-page" : ""}" ${tip ? `title="${tip}"` : ""}  onclick="previewPendingImage(${i})"><img src="${img.preview}" alt="preview">${srcLabel ? `<div class="pdf-badge">${srcLabel}</div>` : ""}<button class="remove-img" onclick="event.stopPropagation();removeImage(${i})">✕</button><div class="img-size">${label}</div></div>`;
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
function removePdfText(i) {
  const entry = pendingPdfTexts[i];
  if (entry) entry._vlmAlive = false; // ★ Kill VLM polling for this entry
  pendingPdfTexts.splice(i, 1);
  renderImagePreviews();
}

// ── VLM PDF async parse (shared core) ────────────────
// Generic VLM parse: works for both main input and edit mode.
// @param file      - File object
// @param entry     - pdf entry object to mutate (vlmStatus, text, etc.)
// @param isAlive   - () => boolean, returns false if entry was removed/cancelled
// @param onUpdate  - () => void, called after each entry mutation to refresh UI
window._vlmParseEntry = async function(file, entry, isAlive, onUpdate) {
  if (!entry) return;
  entry.vlmStatus = "parsing";
  onUpdate();
  try {
    const fd = new FormData();
    fd.append("file", file);
    const startResp = await fetch(apiUrl("/api/pdf/vlm-parse"), {
      method: "POST",
      body: fd,
    });
    if (!startResp.ok) {
      console.warn("[VLM] start failed:", startResp.status);
      entry.vlmStatus = "unavailable";
      onUpdate();
      return;
    }
    const { taskId } = await startResp.json();
    if (!taskId) {
      entry.vlmStatus = "unavailable";
      onUpdate();
      return;
    }
    // Poll for result
    for (let i = 0; i < 150; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      // entry may have been removed
      if (!isAlive()) return;
      try {
        const poll = await fetch(apiUrl(`/api/pdf/vlm-parse/${taskId}`));
        if (!poll.ok) break;
        const task = await poll.json();
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
            // VLM lost all tables — keep the regular parse
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
  } catch (err) {
    console.warn("[VLM] error:", err);
    if (isAlive()) {
      entry.vlmStatus = "unavailable";
      onUpdate();
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
    `<img src="${src}" alt="Preview" class="preview-image">`;
  document.getElementById("previewModal").classList.add("open");
}
function openTextPreview(title, meta, text) {
  document.getElementById("previewBody").innerHTML =
    `<div class="preview-text-panel"><div class="preview-text-header"><span class="preview-text-title">${escapeHtml(title)}</span><span class="preview-text-meta">${escapeHtml(meta)}</span></div><pre class="preview-text-body">${escapeHtml(text)}</pre></div>`;
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
    const rounds = msg.searchRounds || [];
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
      const allRounds = getSearchRoundsFromMsg(msg);
      if (allRounds.length > 0) {
        trunc.remove();
        body.innerHTML = '';
        for (const round of allRounds) {
          const slot = document.createElement('div');
          slot.setAttribute('data-prn', round.roundNum);
          slot.innerHTML = typeof _renderUnifiedToolLine === 'function'
            ? _renderUnifiedToolLine(round, false)
            : `<div class="ptool-line"><span class="ptool-text">${escapeHtml(round.toolName || round.query || '')}</span></div>`;
          body.appendChild(slot);
        }
        return;
      }
    }
  }
  // Fallback: just remove the truncation bar
  trunc.remove();
});
