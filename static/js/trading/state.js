/* ═══════════════════════════════════════════════════════════
   trading/state.js — Shared state, config, utilities
   ═══════════════════════════════════════════════════════════ */

// Namespace that all trading modules attach to
window.TradingApp = window.TradingApp || {};

(function (F) {
  "use strict";

  // ── Base path for API calls (handles reverse-proxy) ──
  const BASE_PATH = (() => {
    const p = window.location.pathname;
    return p.replace(/\/trading\.html$/i, "").replace(/\/+$/, "");
  })();

  F._API = BASE_PATH + "/api/trading";
  F._$ = (id) => document.getElementById(id);

  // ── Shared mutable state ──
  F._state = {
    holdings: [],
    cash: 0,
    strategies: [],
    strategyGroups: [],
    intel: [],
    currentPage: "dashboard",
    currentIntelCat: "all",
    currentStratType: "all",
    currentBtMode: "portfolio",
    lastBtResult: null,
    lastBatchId: null,
    pendingTrades: [],
    refreshTimer: null,
    intelPollTimer: null,
    btTradeFilter: "all",
    intelSort: "time",
    intelSentimentFilter: "",
  };

  // ── API helper ──
  F.api = async function (path, opts = {}) {
    const url = `${F._API}${path}`;
    const defaults = { headers: { "Content-Type": "application/json" } };
    const res = await fetch(url, { ...defaults, ...opts });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${res.status}`);
    }
    return res.json();
  };

  // ── Toast notifications ──
  F.toast = function (msg, type = "info") {
    const container = F._$("toastContainer");
    if (!container) return;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = msg;
    container.appendChild(el);
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 300);
    }, 3000);
  };

  // ── Formatters ──
  F.fmtNum = function (n, decimals = 2) {
    if (n == null || isNaN(n)) return "--";
    return Number(n).toLocaleString("zh-CN", {
      minimumFractionDigits: decimals,
      maximumFractionDigits: decimals,
    });
  };

  F.fmtPct = function (n) {
    if (n == null || isNaN(n)) return "--";
    const s = Number(n).toFixed(2);
    return (n >= 0 ? "+" : "") + s + "%";
  };

  F.pnlClass = function (n) {
    if (n > 0) return "up";
    if (n < 0) return "down";
    return "";
  };

  F.escHtml = function (s) {
    if (!s) return "";
    return s
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  };

  F.timeAgo = function (isoStr) {
    try {
      const d = new Date(isoStr);
      const now = new Date();
      const diffMs = now - d;
      const diffH = diffMs / 3600000;
      if (diffH < 1) return `${Math.floor(diffMs / 60000)}分钟前`;
      if (diffH < 24) return `${Math.floor(diffH)}小时前`;
      const diffD = Math.floor(diffH / 24);
      if (diffD < 7) return `${diffD}天前`;
      return d.toLocaleDateString("zh-CN", { month: "short", day: "numeric" });
    } catch {
      return isoStr ? isoStr.slice(0, 10) : "--";
    }
  };

  F.simpleMarkdown = function (md) {
    if (!md) return "";
    let html = F.escHtml(md);
    html = html.replace(/^### (.+)$/gm, "<h4>$1</h4>");
    html = html.replace(/^## (.+)$/gm, "<h3>$1</h3>");
    html = html.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    html = html.replace(/\*(.+?)\*/g, "<em>$1</em>");
    html = html.replace(/^- (.+)$/gm, "<li>$1</li>");
    html = html.replace(/(<li>.*<\/li>\n?)+/g, "<ul>$&</ul>");
    html = html.replace(/\n\n/g, "</p><p>");
    html = html.replace(/\n/g, "<br>");
    return '<div class="markdown-body"><p>' + html + "</p></div>";
  };

  // renderMarkdown: use 'marked' library if loaded, else fallback to simpleMarkdown
  F.renderMarkdown = function (text) {
    if (!text) return "";
    if (typeof marked !== "undefined") {
      try {
        return marked.parse(text);
      } catch (e) {}
    }
    return text.replace(/\n/g, "<br>");
  };
})(window.TradingApp);
