/* ═══════════════════════════════════════════════════════════
   trading/init.js — Navigation and initialization

   4-tab user-facing navigation:
     首页(overview) → 模拟验证(simulator) → AI帮我选(brain) → 我的持仓(dashboard)

   All expert/internal pages (radar, decision, screening, strategy,
   intel, market, autopilot, history) have been REMOVED from the UI.
   AI handles screening/selection internally — users never see it.
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  "use strict";
  var $ = F._$,
    S = F._state;

  // Only these 4 pages exist in the UI
  var VALID_PAGES = { overview: 1, simulator: 1, brain: 1, dashboard: 1 };

  function navigate(page) {
    // Guard: only allow the 4 user-facing pages
    if (!VALID_PAGES[page]) {
      console.warn('[Nav] Unknown page: ' + page + ', redirecting to overview');
      page = 'overview';
    }

    S.currentPage = page;

    // Hide all pages, show selected
    document.querySelectorAll(".page").forEach(function (p) {
      p.classList.remove("active");
    });
    var target = $("page-" + page);
    if (target) target.classList.add("active");

    // Update nav tabs (only the 4 main tabs)
    document.querySelectorAll(".nav-tab").forEach(function (t) {
      t.classList.toggle("active", t.dataset.page === page);
    });

    // Stop timers when leaving pages
    if (page !== "dashboard" && F.stopRefresh) F.stopRefresh();

    // Load data for the target page
    switch (page) {
      case "overview":
        if (F.loadOverview) F.loadOverview();
        break;
      case "dashboard":
        if (F.loadHoldings) F.loadHoldings();
        if (F.loadHistory) F.loadHistory();
        if (F.startRefresh) F.startRefresh();
        break;
      case "brain":
        if (F.loadBrainState) F.loadBrainState();
        break;
      case "simulator":
        if (F.loadSimulator) F.loadSimulator();
        break;
    }
  }

  function init() {
    // Resume any background tasks from previous session
    if (F.resumeActiveTasks) F.resumeActiveTasks();
    // Initial load — start with overview (the trust-building home page)
    navigate("overview");
  }

  // ── Auto-init ──
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  F.navigate = navigate;
})(window.TradingApp);
