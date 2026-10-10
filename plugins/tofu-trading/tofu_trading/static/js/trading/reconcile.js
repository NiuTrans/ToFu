/* ═══════════════════════════════════════════════════════════
   trading/reconcile.js — the daily reconcile view

   This is the page the user actually opens each day. It answers one
   question: "given where I am now, what should I do?" — recomputed on every
   load from GET /reconcile/plan, never read from a stored command list.

   Three things here are contractual, not cosmetic:

   1. AN EMPTY PLAN MUST EXPLAIN ITSELF. The backend returns `skipped[]`
      where each entry names the gate that rejected it plus a human note.
      Rendering only `actions[]` would give a blank screen on the (common)
      day when everything is within the no-trade band, and the user would
      reasonably conclude the feature is broken.

   2. ADOPTION CAPTURE TAKES NUMBERS, NOT A BOOLEAN. "Done" prompts for the
      actual price/shares because the gap between advised and executed
      (slippage) is the raw material for ever measuring whether the advice
      was any good. A checkbox would make that permanently uncomputable.

   3. PRICES ARE LABELLED AS ESTIMATES. Intraday NAV is unavailable (both
      fundgz domains measured dead — docs/REDESIGN.md §5), so the payload
      carries is_estimate/price_basis and this renderer surfaces it. The UI
      must never imply a live quote it does not have.
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  "use strict";

  var $ = F._$;

  // Gate → human-facing label. Keys mirror the backend's `gate` values;
  // an unknown gate falls back to the raw key rather than being hidden,
  // so a backend that grows a new gate degrades visibly instead of silently.
  var GATE_LABEL = {
    deadband: "免交易带",
    min_ticket: "低于最小票",
    min_ticket_after_lot: "取整后低于最小票",
    in_flight: "有在途份额",
    below_one_lot: "不足一手",
    price_missing: "无可用价格",
  };

  F._reconcile = { plan: null };

  // ── Plan ────────────────────────────────────────────────

  F.loadReconcile = function () {
    var host = $("recPlanBody");
    if (!host) return;
    host.innerHTML = '<div class="rec-loading">正在按当前持仓重新计算…</div>';

    return F.api("/reconcile/plan")
      .then(function (data) {
        F._reconcile.plan = data;
        renderPlan(data);
        return data;
      })
      .catch(function (e) {
        console.warn("[Reconcile] plan load failed:", e && e.message);
        host.innerHTML =
          '<div class="rec-empty"><div class="rec-empty-title">无法获取计划</div>' +
          '<div class="rec-empty-note">' +
          F.escHtml((e && e.message) || "请求失败") +
          "</div></div>";
      });
  };

  function renderPlan(data) {
    var host = $("recPlanBody");
    if (!host) return;

    var actions = (data && data.actions) || [];
    var skipped = (data && data.skipped) || [];
    var html = "";

    // ── Estimate banner: shown whenever prices are not live, which on this
    //    deployment is always. Stated once at the top rather than repeated
    //    per row. ──
    if (data && data.is_estimate) {
      html +=
        '<div class="rec-estimate-banner" id="recEstimateBanner">' +
        '<span class="rec-estimate-icon">≈</span>' +
        F.escHtml(data.estimate_note || "估算（基于上一交易日收盘/净值），非实时") +
        "</div>";
    }

    if (actions.length) {
      html += '<div class="rec-section-title">今日建议操作</div>';
      html += '<div class="rec-action-list">';
      actions.forEach(function (a) {
        html += renderAction(a, data.plan_date);
      });
      html += "</div>";
    } else {
      // ★ The explained empty state. `skipped` tells the user WHY.
      html +=
        '<div class="rec-empty"><div class="rec-empty-title">今天无需操作</div>' +
        '<div class="rec-empty-note">当前持仓与目标的偏离都没有超过阈值。' +
        "漏看几天也没关系 —— 这个清单每次打开都会用最新价格重算。</div></div>";
    }

    if (skipped.length) {
      html += '<details class="rec-skipped" id="recSkippedBlock">';
      html +=
        '<summary class="rec-skipped-summary">为什么其它标的没有出现（' +
        skipped.length +
        "）</summary>";
      html += '<ul class="rec-skipped-list">';
      skipped.forEach(function (s) {
        var label = GATE_LABEL[s.gate] || s.gate || "未知";
        html +=
          '<li class="rec-skipped-item" data-gate="' +
          F.escHtml(s.gate || "") +
          '"><span class="rec-skipped-symbol">' +
          F.escHtml(s.symbol || "") +
          '</span><span class="rec-skipped-gate">' +
          F.escHtml(label) +
          '</span><span class="rec-skipped-note">' +
          F.escHtml(s.note || "") +
          "</span></li>";
      });
      html += "</ul></details>";
    }

    host.innerHTML = html;
  }

  function renderAction(a, planDate) {
    var sideCls = a.side === "buy" ? "buy" : "sell";
    var sideText = a.side === "buy" ? "买入" : "卖出";
    return (
      '<div class="rec-action" data-symbol="' +
      F.escHtml(a.symbol) +
      '" data-side="' +
      F.escHtml(a.side) +
      '">' +
      '<div class="rec-action-main">' +
      '<div class="rec-action-head">' +
      '<span class="rec-side ' + sideCls + '">' + sideText + "</span>" +
      '<span class="rec-symbol">' + F.escHtml(a.symbol) + "</span>" +
      "</div>" +
      '<div class="rec-action-detail">' +
      F.fmtNum(a.shares, 0) + " 份 · ¥" + F.fmtNum(a.amount) +
      '<span class="rec-price">（估算价 ¥' + F.fmtNum(a.price) + "）</span>" +
      "</div>" +
      '<div class="rec-action-reason">' + F.escHtml(a.reason || "") + "</div>" +
      "</div>" +
      '<div class="rec-action-actions">' +
      '<button class="btn-done" onclick="TradingApp.markAction(\'' +
      F.escHtml(planDate) + "','" + F.escHtml(a.symbol) +
      "','done')\">已执行</button>" +
      '<button class="btn-skip" onclick="TradingApp.markAction(\'' +
      F.escHtml(planDate) + "','" + F.escHtml(a.symbol) +
      "','skipped')\">跳过</button>" +
      "</div></div>"
    );
  }

  // ── Adoption capture ────────────────────────────────────

  /**
   * Record what the user actually did.
   *
   * For 'done' we ASK for the real price and share count instead of assuming
   * the advised numbers were filled exactly. Recording the advised figures as
   * if they were executed would quietly fabricate a perfect fill history and
   * destroy the only signal that makes advice quality measurable.
   */
  F.markAction = function (planDate, symbol, status) {
    var body = { status: status };

    if (status === "done") {
      var plan = F._reconcile.plan || {};
      var advised = ((plan.actions || []).filter(function (a) {
        return a.symbol === symbol;
      })[0]) || {};

      var px = window.prompt(
        "成交价（留空则用建议价 ¥" + F.fmtNum(advised.price) + "）",
        advised.price != null ? String(advised.price) : ""
      );
      if (px === null) return;          // cancelled — record nothing
      var sh = window.prompt(
        "成交份数（留空则用建议份数 " + F.fmtNum(advised.shares, 0) + "）",
        advised.shares != null ? String(advised.shares) : ""
      );
      if (sh === null) return;

      body.actual_price = parseFloat(px) || advised.price || 0;
      body.actual_shares = parseFloat(sh) || advised.shares || 0;
    }

    return F.api(
      "/reconcile/action/" + encodeURIComponent(planDate) + "/" +
        encodeURIComponent(symbol) + "/status",
      { method: "POST", body: body }
    )
      .then(function () {
        F.toast(status === "done" ? "已记录执行" : "已记录跳过", "success");
        return F.loadReconcile();
      })
      .catch(function (e) {
        console.warn("[Reconcile] status write failed:", e && e.message);
        F.toast("记录失败：" + ((e && e.message) || "未知错误"), "error");
      });
  };

  // ── Targets: AI proposes, owner approves ────────────────

  F.loadTargets = function () {
    var host = $("recTargetBody");
    if (!host) return;
    host.innerHTML = '<div class="rec-loading">加载中…</div>';

    return F.api("/reconcile/target")
      .then(function (data) {
        var targets = (data && data.targets) || [];
        if (!targets.length) {
          host.innerHTML =
            '<div class="rec-empty"><div class="rec-empty-title">还没有目标组合</div>' +
            '<div class="rec-empty-note">目标组合决定了对账基准。' +
            "AI 提议后需要你批准才会生效。</div></div>";
          return;
        }
        var html = '<div class="rec-target-list">';
        targets.forEach(function (t) {
          // Unapproved rows are visually distinct because they do NOT drive
          // the plan — showing them identically would imply the portfolio is
          // already tracking a target it is actually ignoring.
          var pending = !t.approved;
          html +=
            '<div class="rec-target' + (pending ? " pending" : "") +
            '" data-symbol="' + F.escHtml(t.symbol) + '">' +
            '<div class="rec-target-main">' +
            '<span class="rec-symbol">' + F.escHtml(t.symbol) + "</span>" +
            '<span class="rec-target-weight">' + F.fmtNum(t.target_weight, 1) + "%</span>" +
            (pending ? '<span class="rec-badge-pending">待批准</span>' : "") +
            "</div>" +
            '<div class="rec-target-reason">' + F.escHtml(t.rationale || "") + "</div>" +
            (pending
              ? '<button class="btn-approve" onclick="TradingApp.approveTarget(\'' +
                F.escHtml(t.symbol) + "')\">批准</button>"
              : "") +
            "</div>";
        });
        html += "</div>";
        html +=
          '<div class="rec-target-summary">已批准合计 ' +
          F.fmtNum(data.approved_weight_sum, 1) + "% · 隐含现金 " +
          F.fmtNum(data.implied_cash_weight, 1) + "%</div>";
        host.innerHTML = html;
      })
      .catch(function (e) {
        console.warn("[Reconcile] target load failed:", e && e.message);
        host.innerHTML =
          '<div class="rec-empty"><div class="rec-empty-title">无法获取目标组合</div></div>';
      });
  };

  F.approveTarget = function (symbol) {
    return F.api(
      "/reconcile/target/" + encodeURIComponent(symbol) + "/approve",
      { method: "POST", body: {} }
    )
      .then(function () {
        F.toast("已批准 " + symbol, "success");
        return Promise.all([F.loadTargets(), F.loadReconcile()]);
      })
      .catch(function (e) {
        console.warn("[Reconcile] approve failed:", e && e.message);
        F.toast("批准失败：" + ((e && e.message) || "未知错误"), "error");
      });
  };

  // ── Adoption stats ──────────────────────────────────────

  F.loadAdoption = function () {
    var host = $("recAdoptionBody");
    if (!host) return;
    return F.api("/reconcile/adoption")
      .then(function (data) {
        var rate = data && data.follow_through_rate;
        host.innerHTML =
          '<div class="rec-adoption">' +
          '<span class="rec-adoption-rate">' +
          (rate == null ? "--" : F.fmtNum(rate, 1) + "%") +
          "</span>" +
          '<span class="rec-adoption-label">建议采纳率（' +
          ((data && data.total) || 0) + " 条）</span></div>";
      })
      .catch(function (e) {
        console.debug("[Reconcile] adoption stats unavailable:", e && e.message);
      });
  };

  F.loadReconcilePage = function () {
    F.loadReconcile();
    F.loadTargets();
    F.loadAdoption();
  };
})(window.TradingApp);
