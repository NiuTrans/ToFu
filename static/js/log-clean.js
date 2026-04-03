/* ═══════════════════════════════════════════
   log-clean.js — Log Noise Detection & Cleaning
   ═══════════════════════════════════════════ */
var _pendingLogClean = null;  // shared with main.js — must be var for cross-script access
// ══════════════════════════════════════════════════════
//  ★ Log Noise Detection & Cleaning (Multi-pass)
// ══════════════════════════════════════════════════════

// --- Pass 1: Strip per-line log prefixes (timestamps, worker tags, etc.) ---
// These prefixes vary in timestamp/pid but follow the same pattern.
// We normalize them to a canonical form, not exact-match.
const _logPrefixRegexes = [
  // Ray/vLLM worker: (Worker_XXX pid=NNN) LEVEL MM-DD HH:MM:SS [path:line]
  {
    re: /^\([\w_]+ pid=\d+\)\s+(?:ERROR|WARNING|INFO|DEBUG)\s+\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+\[[^\]]+\]\s*/,
    label: "Worker日志前缀",
  },
  // Bare Ray worker tag: (WorkerName pid=NNN) — heuristic catch-all for
  // traceback lines, error output, etc. that don't have the full log format.
  // Must come AFTER the specific pattern so it only catches the leftovers.
  // Only strips the tag itself (no trailing \s*) to preserve traceback indentation.
  { re: /^\([\w_]+ pid=\d+\) ?/, label: "Worker前缀" },
  // Standard Python logging: LEVEL YYYY-MM-DD HH:MM:SS,NNN module
  {
    re: /^(?:ERROR|WARNING|INFO|DEBUG)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s+[\w.]+\s*/,
    label: "Python日志前缀",
  },
  // Bracketed timestamp: [YYYY-MM-DD HH:MM:SS] LEVEL
  {
    re: /^\[\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}[.,]?\d*\]\s*(?:ERROR|WARNING|INFO|DEBUG|CRITICAL)?\s*/,
    label: "时间戳前缀",
  },
  // Dash-separated: YYYY-MM-DD HH:MM:SS,NNN - name - LEVEL -
  {
    re: /^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}[.,]\d+\s+-\s+[\w.]+\s+-\s+\w+\s+-\s*/,
    label: "日志前缀",
  },
  // Go-style: I0302 01:26:07.123456 file.go:123]
  {
    re: /^[IWEF]\d{4}\s+\d{2}:\d{2}:\d{2}\.\d+\s+\S+\]\s*/,
    label: "Go日志前缀",
  },
  // Task ID prefix: [Task xxxxxxxx] — keeps [Rn] round tags that may follow
  { re: /^\[Task\s+[0-9a-f]+\]\s*/, label: "Task ID前缀" },
  // Flask/Werkzeug access log prefix: 127.0.0.1 - - [DD/Mon/YYYY HH:MM:SS]
  {
    re: /^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s*/,
    label: "HTTP日志前缀",
  },
  // Docker/K8s ISO timestamp prefix: 2024-01-01T00:00:00.000000000Z
  {
    re: /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?\s+/,
    label: "ISO时间戳前缀",
  },
  // systemd/journald: Jan 01 00:00:00 hostname service[pid]:
  {
    re: /^[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\S+\s+\S+(?:\[\d+\])?:\s*/,
    label: "syslog前缀",
  },
];

// --- Noise line patterns: entire lines removed when mixed with other log types ---
const _noiseLineRegexes = [
  // HTTP access log with 2xx/3xx status — successful requests are pure noise when mixed with errors
  // e.g. 127.0.0.1 - - [03/Mar/2026 00:47:26] "POST /api/chat/start HTTP/1.1" 200 -
  {
    re: /^\d{1,3}(?:\.\d{1,3}){3}\s+-\s+\S+\s+\[.*?\]\s+"[A-Z]+\s+\S+\s+HTTP\/[\d.]+"\s+[23]\d{2}\s+[\d-]+\s*$/,
    label: "HTTP成功请求",
  },
];

// --- Pass 2: Remove "pointer" lines (^^^^) that just underline the prev line ---
const _pointerLineRe = /^\s*[\^~]+\s*$/;

// --- Pass 3: Shorten absolute paths ---
// /very/long/path/to/project/foo/bar.py → .../foo/bar.py
// Keeps the last N meaningful path segments
function _shortenPaths(line) {
  return line.replace(
    /(?:\/[\w._-]+){4,}\/([\w._-]+\/[\w._-]+(?:\.[\w]+)?)/g,
    (full, tail) => {
      // Don't shorten if the total is already short
      if (full.length < 50) return full;
      return ".../" + tail;
    },
  );
}

// --- Pass 4: Deduplicate identical worker tracebacks ---
// Multiple workers often produce the exact same traceback.
// We group by content (after prefix stripping) and merge.
function _deduplicateWorkerBlocks(lines) {
  const workerRe = /^\([\w_]+ pid=\d+\)/;
  const workerIds = new Set();
  for (const l of lines) {
    const m = l.match(workerRe);
    if (m) workerIds.add(m[0]);
  }
  if (workerIds.size < 2)
    return { lines, deduped: 0, workerCount: workerIds.size };

  // Split into contiguous worker blocks
  const blocks = []; // { worker, lines[] }
  let cur = null;
  for (const l of lines) {
    const m = l.match(workerRe);
    const wid = m ? m[0] : null;
    if (wid && wid !== cur?.worker) {
      if (cur) blocks.push(cur);
      cur = { worker: wid, lines: [l] };
    } else {
      if (!cur) cur = { worker: wid || "__none__", lines: [] };
      cur.lines.push(l);
    }
  }
  if (cur) blocks.push(cur);

  // Hash each block's content (after stripping prefix) for comparison
  function blockContent(b) {
    return b.lines
      .map((l) => {
        for (const { re } of _logPrefixRegexes) {
          const m = l.match(re);
          if (m) return l.slice(m[0].length);
        }
        return l;
      })
      .join("\n")
      .trim();
  }

  const contentMap = new Map(); // content -> { workers[], firstBlock }
  for (const b of blocks) {
    if (b.worker === "__none__") continue;
    const c = blockContent(b);
    if (!contentMap.has(c)) {
      contentMap.set(c, { workers: [b.worker], firstBlock: b });
    } else {
      contentMap.get(c).workers.push(b.worker);
    }
  }

  let totalDeduped = 0;
  for (const [, v] of contentMap) {
    if (v.workers.length > 1) totalDeduped += v.workers.length - 1;
  }
  if (totalDeduped === 0)
    return { lines, deduped: 0, workerCount: workerIds.size };

  // Rebuild: keep first occurrence, annotate with all worker names
  const usedContents = new Set();
  const result = [];
  for (const b of blocks) {
    if (b.worker === "__none__") {
      result.push(...b.lines);
      continue;
    }
    const c = blockContent(b);
    const info = contentMap.get(c);
    if (!info) {
      result.push(...b.lines);
      continue;
    }
    if (usedContents.has(c)) continue; // skip duplicate
    usedContents.add(c);
    result.push(
      ...b.lines.filter((l) => {
        const m = l.match(workerRe);
        return !m || m[0] === info.workers[0]; // keep first worker's lines
      }),
    );
  }

  return { lines: result, deduped: totalDeduped, workerCount: workerIds.size };
}

// --- Helpers: Device ID extraction for multi-GPU/worker annotations ---
function _extractDeviceIds(lines) {
  const ids = new Set();
  const patterns = [
    /\bcuda:(\d+)/g,
    /\bWorker\s*(\d+)/gi,
    /\bGPU\s*[:_]?\s*(\d+)/gi,
    /\brank\s*[:_]?\s*(\d+)/gi,
    /\bdevice\s*[:_]?\s*(\d+)/gi,
  ];
  for (const line of lines) {
    for (const re of patterns) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(line))) ids.add(parseInt(m[1]));
    }
  }
  return [...ids].sort((a, b) => a - b);
}

function _formatDeviceRange(ids) {
  if (ids.length === 0) return '';
  if (ids.length === 1) return String(ids[0]);
  const ranges = [];
  let start = ids[0], end = ids[0];
  for (let i = 1; i < ids.length; i++) {
    if (ids[i] === end + 1) {
      end = ids[i];
    } else {
      ranges.push(start === end ? String(start) : `${start}-${end}`);
      start = end = ids[i];
    }
  }
  ranges.push(start === end ? String(start) : `${start}-${end}`);
  return ranges.join(', ');
}

// --- Pass 3.3: Collapse tqdm / progress bar output ---
// Detects tqdm-style bars and keeps only start (~0%), middle, and end (max%).
const _tqdmBarRe = /(\d+)%\|[^|]*\|\s*[\d.]+[kKMGT]?\s*\/\s*[\d.]+[kKMGT]?/;

function _isTqdmLine(line) {
  // Must contain N%|bar|N/N AND end with rate-bracket (it/s, s/it, etc.)
  // Lines with extra content after the bar (e.g. [Worker 0] Starting...)
  // are excluded — they carry unique per-device info.
  return _tqdmBarRe.test(line) && /\/(?:s|it)\]\s*$/.test(line);
}

function _extractTqdmPct(line) {
  const m = line.match(_tqdmBarRe);
  return m ? parseInt(m[1]) : -1;
}

function _collapseProgressBars(lines) {
  const result = [];
  let collapsed = 0;
  let i = 0;

  while (i < lines.length) {
    if (!_isTqdmLine(lines[i])) {
      result.push(lines[i]);
      i++;
      continue;
    }

    // Collect consecutive tqdm lines (allow blanks in between)
    const group = [];
    let j = i;
    while (j < lines.length) {
      if (_isTqdmLine(lines[j])) {
        group.push({ line: lines[j], pct: _extractTqdmPct(lines[j]), idx: j });
        j++;
      } else if (lines[j].trim() === '' && j + 1 < lines.length && _isTqdmLine(lines[j + 1])) {
        j++; // skip blank between tqdm lines
      } else {
        break;
      }
    }

    if (group.length < 4) {
      // Too few to collapse — keep as-is
      for (let k = i; k < j; k++) result.push(lines[k]);
      i = j;
      continue;
    }

    // Sample: start, middle, end by percentage
    const minPct = Math.min(...group.map(g => g.pct));
    const maxPct = Math.max(...group.map(g => g.pct));
    const midPct = Math.round((minPct + maxPct) / 2);

    function closestTo(target) {
      let best = group[0], bestDist = Math.abs(best.pct - target);
      for (const g of group) {
        const d = Math.abs(g.pct - target);
        if (d < bestDist || (d === bestDist && g.idx > best.idx)) {
          best = g; bestDist = d;
        }
      }
      return best;
    }

    const picks = [closestTo(minPct)];
    const mid = closestTo(midPct);
    const end = closestTo(maxPct);
    if (mid.idx !== picks[0].idx && mid.idx !== end.idx) picks.push(mid);
    if (end.idx !== picks[0].idx) picks.push(end);
    picks.sort((a, b) => a.idx - b.idx);

    // Detect devices in the group
    const devIds = _extractDeviceIds(group.map(g => g.line));
    const dropped = group.length - picks.length;

    // Emit: first sample, summary, remaining samples
    result.push(picks[0].line);
    let summary = `  … (${dropped} more progress updates`;
    if (devIds.length > 1) summary += `, ×${devIds.length} devices`;
    summary += `) …`;
    result.push(summary);
    for (let k = 1; k < picks.length; k++) result.push(picks[k].line);

    collapsed += dropped;
    i = j;
  }

  return { lines: result, collapsed };
}

// --- Pass 3.5: Collapse similar lines (tuned — conservative fingerprint) ---
// Fingerprint: only normalise truly instance-specific noise (hex addrs,
// UUIDs, IPs, long numeric IDs, device/worker IDs).  Quoted strings,
// qualified names, and short numbers are kept verbatim so lines with
// different error messages or codes are NOT merged.
function _fingerprint(line) {
  return line
    .replace(/0x[0-9a-fA-F]+/g, '⊕')           // hex addresses
    .replace(/[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}/gi, '⊕') // UUIDs
    .replace(/\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?/g, '⊛')  // IP(:port)
    .replace(/\b(?:cuda|gpu|device|worker|rank)\s*[:_]?\s*\d+/gi, '⊗')  // device/worker IDs
    .replace(/\b\d{6,}\b/g, '⊘')               // long numeric IDs (≥6 digits)
    .trim();
}

function _collapseSimilarLines(lines) {
  // --- Pass A: consecutive runs of identical fingerprint (≥ 5 lines) ---
  let result = [];
  let i = 0;
  let collapsed = 0;
  while (i < lines.length) {
    const fp = _fingerprint(lines[i]);
    let j = i + 1;
    while (j < lines.length && _fingerprint(lines[j]) === fp) j++;
    const runLen = j - i;
    if (runLen >= 5 && fp) {
      // Keep first line, annotate with device info if applicable
      result.push(lines[i]);
      const runLines = lines.slice(i, j);
      const devIds = _extractDeviceIds(runLines);
      const droppedA = runLen - 1;
      let summaryA = `  … (${droppedA} more similar`;
      if (devIds.length > 1) summaryA += `, ×${devIds.length} devices: ${_formatDeviceRange(devIds)}`;
      summaryA += `) …`;
      result.push(summaryA);
      collapsed += droppedA;
    } else {
      for (let k = i; k < j; k++) result.push(lines[k]);
    }
    i = j;
  }

  // --- Pass B: scattered duplicates (same fingerprint ≥ 5 times) ---
  const fpCount = new Map();
  const fpAllLines = new Map(); // fp -> all lines with this fingerprint
  for (const l of result) {
    const fp = _fingerprint(l);
    if (fp) {
      fpCount.set(fp, (fpCount.get(fp) || 0) + 1);
      if (!fpAllLines.has(fp)) fpAllLines.set(fp, []);
      fpAllLines.get(fp).push(l);
    }
  }
  const seen = new Map();
  const passB = [];
  let passBdropped = 0;
  for (const l of result) {
    const fp = _fingerprint(l);
    if (!fp) { passB.push(l); continue; }
    const total = fpCount.get(fp) || 0;
    if (total < 5) { passB.push(l); continue; }
    const kept = seen.get(fp) || 0;
    seen.set(fp, kept + 1);
    if (kept === 0) {
      passB.push(l);
      // Annotate with device info from all occurrences
      const allLines = fpAllLines.get(fp) || [];
      const devIds = _extractDeviceIds(allLines);
      const droppedB = total - 1;
      let summaryB = `  … (${droppedB} more similar`;
      if (devIds.length > 1) summaryB += `, ×${devIds.length} devices: ${_formatDeviceRange(devIds)}`;
      summaryB += `) …`;
      passB.push(summaryB);
      passBdropped += droppedB;
    }
    // else: skip this duplicate
  }
  result = passB;
  collapsed += passBdropped;

  return { lines: result, collapsed };
}

// --- Pass 4: Collapse consecutive blank lines ---
function _collapseBlankLines(lines) {
  const result = [];
  let prevBlank = false;
  for (const l of lines) {
    const blank = l.trim() === "";
    if (blank && prevBlank) continue;
    result.push(l);
    prevBlank = blank;
  }
  return result;
}

// ── Main detection: analyze text, return cleaning result or null ──
function detectLogNoise(text) {
  const lines = text.split("\n");
  if (lines.length < 5) return null;

  const ops = []; // { name, desc }  — what we did
  let workingLines = [...lines];

  // --- Stats for banner ---
  let prefixLinesStripped = 0;
  let pointerLinesRemoved = 0;
  let pathsShortenedCount = 0;
  let workersDeduplicated = 0;
  let workerCount = 0;
  let prefixLabel = "";

  // == Pre-pass: Detect worker blocks on ORIGINAL lines (before prefix strip) ==
  // Must run first because Pass 1 will remove the (Worker_XXX pid=NNN) prefix
  const dedupResult = _deduplicateWorkerBlocks(workingLines);
  workersDeduplicated = dedupResult.deduped;
  workerCount = dedupResult.workerCount;
  if (workersDeduplicated > 0) {
    workingLines = dedupResult.lines;
    ops.push({
      name: "dedup",
      desc: `合并${workersDeduplicated}个Worker的重复堆栈（${workerCount}个Worker）`,
    });
  }

  // == Pass 0.5: Remove entire noise lines (HTTP 2xx/3xx access logs, etc.) ==
  let noiseLinesRemoved = 0;
  {
    const before = workingLines.length;
    workingLines = workingLines.filter((l) => {
      if (!l.trim()) return true; // keep blanks for now
      for (const pat of _noiseLineRegexes) {
        if (pat.re.test(l)) return false;
      }
      return true;
    });
    noiseLinesRemoved = before - workingLines.length;
    if (noiseLinesRemoved > 0) {
      ops.push({
        name: "noise",
        desc: `移除${noiseLinesRemoved}行HTTP成功请求日志`,
      });
    }
  }

  // == Pass 1: Detect & strip per-line prefixes (multi-pattern) ==
  // Apply ALL patterns that match ≥3 lines individually (not just the single best).
  // Each pattern is applied in sequence, stripping its match from the line.
  const totalNonEmpty = workingLines.filter((l) => l.trim()).length;
  const appliedPrefixLabels = [];
  for (const pat of _logPrefixRegexes) {
    let cnt = 0;
    for (const l of workingLines) {
      if (l.trim() && pat.re.test(l)) cnt++;
    }
    // Require ≥3 matches AND ≥15% of non-empty lines (lower bar since we apply multiple)
    if (cnt >= 3 && cnt / Math.max(totalNonEmpty, 1) >= 0.15) {
      prefixLinesStripped += cnt;
      workingLines = workingLines.map((l) => {
        const m = l.match(pat.re);
        return m ? l.slice(m[0].length) : l;
      });
      appliedPrefixLabels.push(`${cnt}行${pat.label}`);
    }
  }
  if (appliedPrefixLabels.length > 0) {
    prefixLabel = appliedPrefixLabels.join("、");
    ops.push({ name: "prefix", desc: `去除${appliedPrefixLabels.join("、")}` });
  }

  // == Pass 2: Remove pointer lines (^^^) ==
  const beforePointer = workingLines.length;
  workingLines = workingLines.filter((l) => !_pointerLineRe.test(l));
  pointerLinesRemoved = beforePointer - workingLines.length;
  if (pointerLinesRemoved > 0) {
    ops.push({
      name: "pointer",
      desc: `移除${pointerLinesRemoved}行指向箭头(^^^)`,
    });
  }

  // == Pass 3: Shorten absolute paths ==
  let totalPathCharsShaved = 0;
  workingLines = workingLines.map((l) => {
    const shortened = _shortenPaths(l);
    totalPathCharsShaved += l.length - shortened.length;
    return shortened;
  });
  if (totalPathCharsShaved > 50) {
    pathsShortenedCount = workingLines.filter((l, i) => l !== lines[i]).length;
    ops.push({
      name: "paths",
      desc: `缩短长路径，节省${totalPathCharsShaved}字符`,
    });
  } else {
    totalPathCharsShaved = 0; // not worth it
    workingLines = workingLines; // keep as-is (already mapped)
  }

  // == Pass 3.3: Collapse tqdm / progress bar output ==
  let progressBarsCollapsed = 0;
  {
    const prog = _collapseProgressBars(workingLines);
    progressBarsCollapsed = prog.collapsed;
    if (progressBarsCollapsed > 0) {
      workingLines = prog.lines;
      ops.push({
        name: 'progress',
        desc: `压缩${progressBarsCollapsed}行进度条（保留首/中/末）`,
      });
    }
  }

  // == Pass 3.5: Collapse similar lines (conservative fingerprint) ==
  let similarLinesCollapsed = 0;
  {
    const sim = _collapseSimilarLines(workingLines);
    similarLinesCollapsed = sim.collapsed;
    if (similarLinesCollapsed > 0) {
      workingLines = sim.lines;
      ops.push({
        name: 'similar',
        desc: `合并${similarLinesCollapsed}行重复/近似日志`,
      });
    }
  }

  // == Pass 4: Collapse consecutive blank lines ==
  const beforeCollapse = workingLines.length;
  workingLines = _collapseBlankLines(workingLines);
  const blankLinesRemoved = beforeCollapse - workingLines.length;
  if (blankLinesRemoved > 2) {
    ops.push({ name: "blanks", desc: `合并${blankLinesRemoved}个连续空行` });
  }

  // --- Calculate savings ---
  if (ops.length === 0) return null;

  const cleanedText = workingLines.join("\n");
  const savedChars = text.length - cleanedText.length;
  const savedPct = Math.round((savedChars / text.length) * 100);

  // Not worth showing if savings are trivial
  if (savedPct < 8 || savedChars < 80) return null;

  // Build a sample of what a prefix looks like (for display)
  let prefixExample = "";
  if (prefixLinesStripped > 0) {
    for (const l of lines) {
      for (const pat of _logPrefixRegexes) {
        const m = l.match(pat.re);
        if (m) {
          prefixExample = m[0].trim();
          break;
        }
      }
      if (prefixExample) break;
    }
  }

  return {
    originalText: text,
    cleanedText,
    ops,
    prefixExample,
    prefixLabel,
    prefixLinesStripped,
    noiseLinesRemoved,
    pointerLinesRemoved,
    pathsShortenedCount,
    similarLinesCollapsed,
    progressBarsCollapsed,
    workersDeduplicated,
    workerCount,
    totalLines: totalNonEmpty,
    savedChars,
    savedPct,
  };
}

function showLogCleanBanner(result) {
  _pendingLogClean = result;
  const banner = document.getElementById("logCleanBanner");
  const info = document.getElementById("logCleanInfo");
  const details = document.getElementById("logCleanDetails");

  // Summary line
  let desc = `检测到日志噪音，可节省 <strong>${result.savedChars.toLocaleString()}</strong> 字符（<strong>${result.savedPct}%</strong>）`;
  info.innerHTML = desc;

  // Detailed breakdown as tags
  let tagsHtml = "";
  for (const op of result.ops) {
    const icons = {
      prefix: "",
      pointer: "",
      paths: "",
      dedup: "",
      similar: "",
      progress: "",
      blanks: "",
      noise: "",
    };
    tagsHtml += `<span class="log-clean-tag">${icons[op.name] || "•"} ${op.desc}</span>`;
  }
  details.innerHTML = tagsHtml;

  banner.style.display = "flex";
}

function hideLogCleanBanner() {
  const banner = document.getElementById("logCleanBanner");
  if (banner) banner.style.display = "none";
  _pendingLogClean = null;
}

// ── Shared helper: get whichever textarea is currently active ──
function _getActiveTextarea() {
  if (_editingMsgIdx !== null) {
    const el = document.getElementById("edit-textarea-" + _editingMsgIdx);
    if (el) return el;
  }
  return document.getElementById("userInput");
}

function applyLogClean() {
  if (!_pendingLogClean) return;
  const input = _getActiveTextarea();
  if (!input) return;
  input.value = input.value.replace(
    _pendingLogClean.originalText,
    _pendingLogClean.cleanedText,
  );
  input.style.height = "auto";
  const maxH = _editingMsgIdx !== null ? 300 : 200;
  input.style.height = Math.min(input.scrollHeight, maxH) + "px";
  debugLog(
    `Log noise cleaned: saved ${_pendingLogClean.savedChars} chars (${_pendingLogClean.savedPct}%)`,
    "success",
  );
  hideLogCleanBanner();
}

async function aiCompressLog() {
  if (!_pendingLogClean) return;
  const btn = document.getElementById("aiCompressBtn");
  if (!btn) return;

  // Use the ORIGINAL text (before any rule-based cleaning) as input to LLM
  const originalText = _pendingLogClean.originalText;

  // Disable button, show loading
  btn.disabled = true;
  const origLabel = btn.textContent;
  btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg> 压缩中…';

  try {
    const resp = await fetch(apiUrl("/api/log/compress"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: originalText }),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || "API error");

    const compressed = data.compressed || "";
    if (!compressed.trim()) throw new Error("LLM returned empty result");

    // Apply directly to textarea
    const input = _getActiveTextarea();
    if (!input) throw new Error("No active textarea");
    input.value = input.value.replace(originalText, compressed);
    input.style.height = "auto";
    const maxH = _editingMsgIdx !== null ? 300 : 200;
    input.style.height = Math.min(input.scrollHeight, maxH) + "px";

    const savedChars = originalText.length - compressed.length;
    const savedPct = Math.round((savedChars / originalText.length) * 100);
    debugLog(
      `AI log compress: ${originalText.length} → ${compressed.length} chars (saved ${savedPct}%, model: ${data.model || "?"})`,
      "success",
    );
    hideLogCleanBanner();
  } catch (err) {
    debugLog(`[AI Compress] failed: ${err.message}`, "error");
    btn.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-1px"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/></svg> 失败，重试';
    btn.disabled = false;
  }
}

function previewLogClean() {
  if (!_pendingLogClean) return;
  const r = _pendingLogClean;
  const beforeLen = r.originalText.length;
  const afterLen = r.cleanedText.length;

  // Build a breakdown section
  let breakdownHtml = '<div class="log-clean-breakdown">';
  for (const op of r.ops) {
    const icons = {
      prefix: "",
      pointer: "",
      paths: "",
      dedup: "",
      similar: "",
      progress: "",
      blanks: "",
      noise: "",
    };
    breakdownHtml += `<div class="log-clean-breakdown-item"><span class="log-clean-breakdown-icon">${icons[op.name] || "•"}</span><span>${op.desc}</span></div>`;
  }
  breakdownHtml += "</div>";

  const previewHtml = `
       <div class="log-clean-compare">
         ${breakdownHtml}
         <div class="log-clean-section">
           <div class="log-clean-section-header">
             <span class="log-clean-section-title">清理前</span>
             <span class="log-clean-section-meta">${beforeLen.toLocaleString()} 字符 · ${r.totalLines} 行</span>
           </div>
           <pre class="log-clean-code">${escapeHtml(r.originalText.slice(0, 3000))}${beforeLen > 3000 ? "\n... (" + (beforeLen - 3000).toLocaleString() + " more chars)" : ""}</pre>
         </div>
         <div class="log-clean-section">
           <div class="log-clean-section-header">
             <span class="log-clean-section-title">清理后</span>
             <span class="log-clean-section-meta">${afterLen.toLocaleString()} 字符 · 节省 ${r.savedPct}%</span>
           </div>
           <pre class="log-clean-code">${escapeHtml(r.cleanedText.slice(0, 3000))}${afterLen > 3000 ? "\n... (" + (afterLen - 3000).toLocaleString() + " more chars)" : ""}</pre>
         </div>
       </div>`;
  document.getElementById("previewBody").innerHTML =
    `<div class="preview-text-panel" style="width:min(900px,90vw)"><div class="preview-text-header"><span class="preview-text-title">日志噪音清理预览</span><span class="preview-text-meta">节省 ${r.savedChars.toLocaleString()} 字符 (${r.savedPct}%)</span></div><div style="padding:16px 20px;overflow-y:auto;max-height:calc(85vh - 60px)">${previewHtml}</div></div>`;
  document.getElementById("previewModal").classList.add("open");
}
