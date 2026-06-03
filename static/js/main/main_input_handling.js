/* ═══════════════════════════════════════════════════════════════════
   main input handling — extracted from main.js (split 2026-05-28)

   Input handling: handleKeyDown, _wrapSelectionNoTranslate, theme toggle, sidebar search.

   This file is concatenated by lib/js_bundler.py BEFORE main.js so
   the boot IIFE can reference these symbols. Symbols share `window`
   scope — no imports / exports needed.
   ═══════════════════════════════════════════════════════════════════ */


function handleKeyDown(e) {
  // IME composition guard: when an IME (e.g. Chinese pinyin) is composing,
  // pressing Enter is meant to commit the candidate / pending input — NOT
  // to send the message. Browsers expose this via `e.isComposing` and the
  // legacy `keyCode === 229` sentinel. Bail out early so the IME handles
  // the keystroke naturally. Matches IM conventions (Feishu / WeChat etc).
  if (e.key === "Enter" && (e.isComposing || e.keyCode === 229)) {
    return;
  }
  // Shift+Enter ALWAYS inserts a newline (let browser default run).
  if (e.key === "Enter" && e.shiftKey && !e.ctrlKey) {
    return;
  }
  const mode = _getSendMode();
  if (mode === 'ctrl_enter') {
    // Ctrl+Enter → send; plain Enter → newline (default behavior, don't prevent).
    if (e.key === "Enter" && e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      _doSendOrGenerate();
      return;
    }
    // plain Enter — let the textarea insert a newline naturally.
  } else {
    // Default 'enter' mode: Enter → send; Ctrl+Enter → newline.
    if (e.key === "Enter" && !e.ctrlKey && !e.shiftKey) {
      e.preventDefault();
      _doSendOrGenerate();
      return;
    }
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      _insertNewlineAtCursor(e.target);
      return;
    }
  }
  // Ctrl+Shift+K — wrap selected text in <notranslate> tags (skip translation)
  if (e.key === "K" && e.ctrlKey && e.shiftKey) {
    e.preventDefault();
    _wrapSelectionNoTranslate(e.target);
    return;
  }
  // Escape exits image gen mode or branch mode
  if (e.key === "Escape") {
    if (imageGenMode) { e.preventDefault(); exitImageGenMode(); return; }
    if (typeof isBranchModeActive === "function" && isBranchModeActive()) {
      e.preventDefault();
      closeBranchPanel();
      return;
    }
  }
}

/**
 * Wrap the selected text in the textarea with <notranslate> tags.
 * If no text is selected, insert an empty <notranslate></notranslate> pair
 * with cursor positioned in the middle.
 */
function _wrapSelectionNoTranslate(textarea) {
  if (!textarea || textarea.tagName !== 'TEXTAREA') return;
  const start = textarea.selectionStart;
  const end = textarea.selectionEnd;
  const text = textarea.value;
  const selected = text.substring(start, end);
  const tag = '<notranslate>';
  const closeTag = '</notranslate>';
  const before = text.substring(0, start);
  const after = text.substring(end);
  textarea.value = before + tag + selected + closeTag + after;
  // Position cursor: if had selection, select the wrapped text; else put cursor inside tags
  if (selected) {
    textarea.selectionStart = start + tag.length;
    textarea.selectionEnd = start + tag.length + selected.length;
  } else {
    textarea.selectionStart = textarea.selectionEnd = start + tag.length;
  }
  textarea.focus();
  // Trigger auto-resize
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
}


// ══════════════════════════════════════════════════════
//  Settings — Tabbed Panel
// ══════════════════════════════════════════════════════

// _serverConfig is declared in settings.js

// ── Settings functions moved to settings.js ──


// ══════════════════════════════════════════════════════
//  Theme System
// ══════════════════════════════════════════════════════
const _THEMES = ["dark", "light", "tofu"];
const _THEME_ICONS = {
  dark: "🌙",
  light: "☀️",
  tofu: "🍮",
};

function _getCurrentTheme() {
  return localStorage.getItem("claude_ui_theme") || "tofu";
}
function applyTheme(theme) {
  if (!_THEMES.includes(theme)) theme = "tofu";
  /* Always set data-theme attribute — no special "no-attribute" default */
  document.documentElement.setAttribute("data-theme", theme);
  localStorage.setItem("claude_ui_theme", theme);
  // Update cycle button icon
  const btn = document.getElementById("themeCycleBtn");
  if (btn) {
    const sp = btn.querySelector("span");
    if (sp) sp.textContent = _THEME_ICONS[theme] || "🌙";
  }
  // Update picker in settings modal
  document.querySelectorAll(".theme-option").forEach((el) => {
    el.classList.toggle("active", el.dataset.theme === theme);
  });
  debugLog(`Theme → ${theme}`, "success");
}
function selectTheme(theme) {
  applyTheme(theme);
}
function cycleTheme() {
  const cur = _getCurrentTheme();
  const idx = _THEMES.indexOf(cur);
  applyTheme(_THEMES[(idx + 1) % _THEMES.length]);
}

/* Cost dashboard aliases — moved to myday.js */

// ── Sidebar search (expandable from header button) ──
function initSidebarSearch() {
  const input = document.getElementById("sidebarSearchInput");
  let timer = null;
  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      sidebarSearchQuery = input.value.trim().toLowerCase();
      /* Exit folder view when searching — search should cover all conversations */
      if (sidebarSearchQuery && typeof getActiveFolderId === 'function' && getActiveFolderId()) {
        setActiveFolderId(null);
      }
      renderConversationList();
    }, 300);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      closeSidebarSearch();
    }
  });
}

/** Toggle sidebar search panel open/closed */
function toggleSidebarSearch() {
  const wrapper = document.getElementById("sidebarSearchWrapper");
  const toggle = document.getElementById("sidebarSearchToggle");
  if (!wrapper) return;
  const isOpen = wrapper.style.display !== "none";
  if (isOpen) {
    closeSidebarSearch();
  } else {
    wrapper.style.display = "";
    if (toggle) toggle.classList.add("active");
    const input = document.getElementById("sidebarSearchInput");
    if (input) { input.focus(); input.select(); }
  }
}

/** Close sidebar search and clear results */
function closeSidebarSearch() {
  const wrapper = document.getElementById("sidebarSearchWrapper");
  const toggle = document.getElementById("sidebarSearchToggle");
  const input = document.getElementById("sidebarSearchInput");
  if (input) input.value = "";
  sidebarSearchQuery = "";
  document.getElementById("sidebarSearchStats")?.classList.remove("visible");
  if (wrapper) wrapper.style.display = "none";
  if (toggle) toggle.classList.remove("active");
  renderConversationList();
}

/* Legacy alias — in case any code calls clearSidebarSearch */
function clearSidebarSearch() { closeSidebarSearch(); }

