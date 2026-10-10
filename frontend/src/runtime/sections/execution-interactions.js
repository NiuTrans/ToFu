/* ===== migrated source: execution-interactions.js ===== */
/* Responsibility: lightweight coding-task interaction presenters.
   Entries: write approvals, subprocess stdin, and apply-code confirmation.
   Dependencies: retained conversation/project state, Api.project/chat, dialogs.
   This section stays retained so common coding actions never load Project UI. */
let _pendingWriteApprovals = new Map();

async function resolveWriteApproval(approvalId, approved) {
  try {
    const data = await Api.project.writeApproval(approvalId, approved);
    if (!data || data.error) {
      debugLog("Approval failed: " + ((data && data.error) || "Unknown"), "warn");
      return;
    }
    debugLog(
      `Write ${approved ? "approved" : "rejected"}: ${approvalId.slice(0, 16)}`,
      approved ? "success" : "warn",
    );
  } catch (e) {
    debugLog("Approval error: " + e.message, "error");
  }
}

// ══════════════════════════════════════════════════════
//  Interactive Stdin — subprocess waiting for keyboard input
// ══════════════════════════════════════════════════════

async function submitStdinInput(stdinId, inputText) {
  if (!stdinId) return;
  try {
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    const data = await Api.chat.stdinResponse(stdinId, inputText, false,
                                              conv && conv.id);
    if (!data || data.error) {
      debugLog("Stdin submit failed: " + ((data && data.error) || "Unknown"), "warn");
      if (typeof showToast === 'function')
        showToast("", "Stdin Error", (data && data.error) || "Failed to send input", 5000);
      return;
    }
    debugLog(`Stdin input sent: ${stdinId}`, "success");
  } catch (e) {
    debugLog("Stdin error: " + e.message, "error");
    if (typeof showToast === 'function')
      showToast("", "Stdin Error", e.message, 5000);
  }
}

async function submitStdinEof(stdinId) {
  // Send EOF flag to signal stdin close
  if (!stdinId) return;
  try {
    const conv = (typeof getActiveConv === 'function') ? getActiveConv() : null;
    const data = await Api.chat.stdinResponse(stdinId, "", true,
                                              conv && conv.id);
    if (!data || data.error) {
      debugLog("Stdin EOF failed: " + ((data && data.error) || "Unknown"), "warn");
      return;
    }
    debugLog(`Stdin EOF sent: ${stdinId}`, "success");
  } catch (e) {
    debugLog("Stdin EOF error: " + e.message, "error");
  }
}

// ══════════════════════════════════════════════════════
//  Apply Code to File
// ══════════════════════════════════════════════════════

let _applyPendingCode = "";

function openApplyModal(btn) {
  if (!projectState.active) {
    debugLog("No project set — cannot apply code", "warn");
    return;
  }
  var pre = btn.closest("pre");
  var code = pre.querySelector("code");
  if (!code) return;
  _applyPendingCode = code.textContent;

  var detectedPath = _detectFilePath(pre, code);
  document.getElementById("applyFilePath").value = detectedPath || "";

  var lines = _applyPendingCode.split("\n");
  var preview =
    lines.length > 20
      ? lines.slice(0, 10).join("\n") +
        "\n  … (" +
        (lines.length - 20) +
        " more lines) …\n" +
        lines.slice(-10).join("\n")
      : _applyPendingCode;
  document.getElementById("applyPreview").innerHTML =
    '<div style="font-size:11px;color:var(--text-tertiary);margin-bottom:4px">' +
    lines.length +
    " lines · " +
    _applyPendingCode.length.toLocaleString() +
    " chars</div>" +
    '<pre style="max-height:300px;overflow:auto;font-size:12px;padding:8px;background:var(--bg-primary);border-radius:6px;margin:0"><code>' +
    escapeHtml(preview) +
    "</code></pre>";

  document.getElementById("applyStatus").innerHTML = "";
  document.getElementById("applyConfirmBtn").disabled = false;
  document.getElementById("applyConfirmBtn").textContent = "Write File";
  document.getElementById("applyModal").classList.add("open");
  setTimeout(function () {
    document.getElementById("applyFilePath").focus();
  }, 100);
}

function closeApplyModal() {
  document.getElementById("applyModal").classList.remove("open");
  _applyPendingCode = "";
}

function _detectFilePath(preEl, codeEl) {
  var firstLine = (codeEl.textContent || "").split("\n")[0] || "";
  var fileCommentMatch = firstLine.match(
    /^(?:#|\/\/|\/\*|<!--)\s*(?:file|path|filename):\s*(.+?)(?:\s*(?:\*\/|-->))?$/i,
  );
  if (fileCommentMatch) return fileCommentMatch[1].trim();
  var node = preEl.previousElementSibling;
  for (var i = 0; i < 3 && node; i++) {
    var text = node.textContent || "";
    var pathMatch = text.match(/`([^`]+\.\w{1,10})`\s*[:：]?\s*$/);
    if (
      pathMatch &&
      (pathMatch[1].indexOf("/") >= 0 || pathMatch[1].indexOf(".") >= 0)
    ) {
      return pathMatch[1];
    }
    var fileMatch = text.match(
      /(?:file|文件)[：:]\s*[`"']?([^\s`"']+\.\w{1,10})/i,
    );
    if (fileMatch) return fileMatch[1];
    node = node.previousElementSibling;
  }
  return "";
}

async function confirmApplyCode() {
  var path = document.getElementById("applyFilePath").value.trim();
  if (!path) {
    document.getElementById("applyStatus").innerHTML =
      '<div style="color:var(--error-text);font-size:12px;margin-top:8px">Please enter a file path</div>';
    return;
  }
  if (!_applyPendingCode) return;

  var btn = document.getElementById("applyConfirmBtn");
  btn.disabled = true;
  btn.textContent = "Writing…";
  document.getElementById("applyStatus").innerHTML = "";

  try {
    var data = await Api.project.write(path, _applyPendingCode);
    if (data && data.ok) {
      var action = data.created ? "Created" : "Updated";
      document.getElementById("applyStatus").innerHTML =
        '<div style="color:#34d399;font-size:12px;margin-top:8px">' +
        action +
        ": " +
        escapeHtml(data.path) +
        " (" +
        data.lines +
        " lines)</div>";
      debugLog(
        "Applied code to " +
          data.path +
          " (" +
          data.lines +
          " lines, " +
          (data.created ? "created" : "updated") +
          ")",
        "success",
      );
      setTimeout(function () {
        closeApplyModal();
      }, 1200);
    } else {
      throw new Error(data.error || "Write failed");
    }
  } catch (e) {
    document.getElementById("applyStatus").innerHTML =
      '<div style="color:var(--error-text);font-size:12px;margin-top:8px">' +
      escapeHtml(e.message) +
      "</div>";
    btn.disabled = false;
    btn.textContent = "Write File";
  }
}
