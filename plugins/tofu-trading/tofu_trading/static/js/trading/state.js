/* ═══════════════════════════════════════════════════════════
   trading/state.js — Shared state, config, utilities
   ═══════════════════════════════════════════════════════════ */

// Namespace that all trading modules attach to
window.TradingApp = window.TradingApp || {};

(function (F) {
  "use strict";

  let host = null;
  F.bindHost = function (ports) {
    if (ports?.version !== 1 || typeof ports.api?.trading?.call !== "function"
        || typeof ports.api?.trading?.raw !== "function"
        || typeof ports.markdown?.parse !== "function"
        || typeof ports.markdown?.sanitize !== "function") {
      throw new Error("Trading requires the v1 frontend host ports");
    }
    host = ports;
  };

  F._$ = (id) => document.getElementById(id);

  // ── Shared mutable state ──
  F._state = {
    holdings: [],
    cash: 0,
    currentPage: "dashboard",
    refreshTimer: null,
  };

  // The host owns transport, API versioning, cancellation and wire envelopes.
  F.api = function (path, opts = {}) {
    if (!host) return Promise.reject(new Error("Api client not loaded"));
    return host.api.trading.call(path, opts);
  };
  F.apiRaw = function (path, opts = {}) {
    if (!host) return Promise.reject(new Error("Api client not loaded"));
    return host.api.trading.raw(path, opts);
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
    } catch (e) {
      console.debug("[Trading] timeAgo parse error:", e.message);
      return isoStr ? isoStr.slice(0, 10) : "--";
    }
  };

  // Model-authored markup is sanitized through the locked host dependency.
  // An unbound host fails closed to escaped text.
  F.sanitizeHtml = function (html) {
    return host ? host.markdown.sanitize(html) : F.escHtml(html);
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
    return F.sanitizeHtml('<div class="markdown-body"><p>' + html + "</p></div>");
  };

  // Parse and sanitize through explicit ports; failures remain inert text.
  F.renderMarkdown = function (text) {
    if (!text) return "";
    if (host) {
      try {
        return F.sanitizeHtml(host.markdown.parse(text));
      } catch (e) {
        console.warn("[Trading] marked.parse failed:", e.message);
      }
    }
    return F.escHtml(text).replace(/\n/g, "<br>");
  };
})(window.TradingApp);
