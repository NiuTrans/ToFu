/* ═══════════════════════════════════════════════════════════
   trading/brain.js — Brain tab: unified AI decision center
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  "use strict";
  const { api, toast, fmtNum, fmtPct, pnlClass, escHtml, _$: $, _state: S } = F;

  let _brainAbort = null;

  /* ────────────────── Outlook helpers ────────────────── */
  const OUTLOOK_MAP = {
    bullish:  { icon: "🟢", label: "看涨",  cls: "outlook-bull" },
    bearish:  { icon: "🔴", label: "看跌",  cls: "outlook-bear" },
    neutral:  { icon: "🟡", label: "观望",  cls: "outlook-neut" },
    cautious: { icon: "🟠", label: "小心",  cls: "outlook-caut" },
  };

  const ACTION_MAP = {
    buy:    { icon: "▲", label: "买入",  cls: "act-buy" },
    add:    { icon: "▲", label: "再多买一些",  cls: "act-buy" },
    sell:   { icon: "▼", label: "卖出",  cls: "act-sell" },
    reduce: { icon: "▼", label: "卖掉一部分",  cls: "act-sell" },
    hold:   { icon: "■", label: "先拿着不动",  cls: "act-hold" },
  };

  const RISK_COLOR = {
    "high-high":   { cls: "risk-crit",  label: "严重" },
    "high-medium": { cls: "risk-high",  label: "高" },
    "medium-high": { cls: "risk-high",  label: "高" },
    "medium-medium":{ cls: "risk-med",  label: "中" },
    "medium-low":  { cls: "risk-low",   label: "低" },
    "low-medium":  { cls: "risk-low",   label: "低" },
    "low-low":     { cls: "risk-low",   label: "低" },
    "high-low":    { cls: "risk-med",   label: "中" },
    "low-high":    { cls: "risk-med",   label: "中" },
  };

  // ═════ Load Brain State ═════

  async function loadBrainState() {
    try {
      const state = await api("/brain/state");
      renderBrainStatus(state);
      renderBrainCycles(state.recent_cycles || []);

      // Auto toggle
      const toggle = $("brainAutoToggle");
      if (toggle) toggle.checked = state.auto_enabled || false;

      // Morning orders
      loadMorningOrders();
    } catch (e) {
      // Fallback to autopilot state
      try {
        if (F.loadAutopilotState) F.loadAutopilotState();
      } catch (e2) {
        toast("加载Brain状态失败: " + e.message, "error");
      }
    }
  }

  function renderBrainStatus(state) {
    const outlookEl = $("brainOutlook");
    const confEl = $("brainConfidence");
    const pendingEl = $("brainPendingTrades");
    const winRateEl = $("brainWinRate");

    if (outlookEl) {
      const cycle =
        state.recent_cycles && state.recent_cycles[0]
          ? state.recent_cycles[0]
          : null;
      const outlook = cycle ? cycle.market_outlook || "--" : "--";
      const info = OUTLOOK_MAP[outlook];
      outlookEl.textContent = info ? `${info.icon} ${info.label}` : outlook;
    }
    if (confEl) {
      const cycle =
        state.recent_cycles && state.recent_cycles[0]
          ? state.recent_cycles[0]
          : null;
      confEl.textContent = cycle ? `${cycle.confidence_score || 0}/100` : "--";
    }
    if (pendingEl) pendingEl.textContent = state.pending_trades || 0;

    if (winRateEl) {
      const stats = state.recommendation_stats || {};
      const total = (stats.correct || 0) + (stats.incorrect || 0);
      winRateEl.textContent =
        total > 0
          ? `${((stats.correct / total) * 100).toFixed(0)}%`
          : "--";
    }

    // Show cycle info
    const infoEl = $("brainCycleInfo");
    if (infoEl && state.last_cycle) {
      infoEl.textContent = `上次分析: ${state.last_cycle}`;
    }
  }

  function renderBrainCycles(cycles) {
    const el = $("brainCyclesList");
    if (!el) return;
    if (!cycles.length) {
      el.innerHTML =
        '<div class="brain-empty-state" style="padding:12px;color:var(--t3)">暂无历史分析</div>';
      return;
    }
    el.innerHTML = cycles
      .map(
        (c) => `
      <div class="brain-cycle-item" onclick="TradingApp.viewBrainCycle('${escHtml(c.cycle_id || c.id)}')">
        <div class="cycle-meta">
          <span class="cycle-id">#${c.cycle_number || c.id}</span>
          <span class="cycle-time">${c.created_at || ""}</span>
        </div>
        <div class="cycle-stats">
          <span class="cycle-outlook">${c.market_outlook || "?"}</span>
          <span class="cycle-confidence">信心: ${c.confidence_score || 0}</span>
          <span class="cycle-status">${c.status || ""}</span>
        </div>
      </div>`,
      )
      .join("");
  }

  // ═════ Morning Orders ═════

  async function loadMorningOrders() {
    try {
      const data = await api("/portfolio/morning");
      const container = $("brainMorningOrders");
      const list = $("brainOrdersList");
      if (!container || !list) return;

      const orders = data.orders?.orders || [];
      if (orders.length === 0) {
        container.style.display = "none";
        return;
      }

      container.style.display = "block";
      list.innerHTML = orders
        .map(
          (o) => `
        <div class="order-item ${o.action === "buy" ? "order-buy" : "order-sell"}">
          <div class="order-meta">
            <span class="order-action">${o.action === "buy" ? "🟢 买入" : "🔴 卖出"}</span>
            <span class="order-symbol">${o.symbol} ${escHtml(o.asset_name || "")}</span>
            <span class="order-amount">¥${fmtNum(o.amount)}</span>
          </div>
          ${o.reason ? `<div class="order-reason">${escHtml(o.reason)}</div>` : ""}
        </div>`,
        )
        .join("");
    } catch (e) {
      // Non-critical
    }
  }

  // ═════ Brain Analysis (Streaming) ═════

  async function runBrainAnalysis() {
    const btn = $("brainAnalyzeBtn");
    const contentEl = $("brainContent");
    const thinkingEl = $("brainThinking");

    if (btn) {
      btn.disabled = true;
      btn.textContent = "🔄 分析中...";
    }
    if (contentEl) contentEl.innerHTML = "";
    if (thinkingEl) {
      thinkingEl.style.display = "none";
      thinkingEl.innerHTML = "";
    }

    // Container for structured result — appended AFTER LLM markdown
    const structuredContainer = document.getElementById("brainStructuredResult");
    if (structuredContainer) {
      structuredContainer.innerHTML = "";
      structuredContainer.style.display = "none";
    }

    try {
      const url = `${F._API}/brain/stream`;
      const resp = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trigger: "manual", scan_candidates: true }),
      });

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let fullContent = "";

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";

        for (const line of lines) {
          if (!line.startsWith("data: ")) continue;
          const payload = line.slice(6).trim();
          if (payload === "[DONE]") continue;

          try {
            const evt = JSON.parse(payload);

            if (evt.thinking) {
              if (thinkingEl) {
                thinkingEl.style.display = "block";
                thinkingEl.textContent += evt.thinking;
              }
            } else if (evt.content) {
              fullContent += evt.content;
              if (contentEl) contentEl.innerHTML = F.renderMarkdown(fullContent);
            } else if (evt.kpi_evaluations) {
              // Render KPI cards later
            } else if (evt.new_candidates) {
              renderBrainCandidates(evt.new_candidates);
            } else if (evt.alerts) {
              renderBrainAlerts(evt.alerts);
            } else if (evt.done) {
              handleBrainDone(evt);
            } else if (evt.error) {
              toast("分析出错: " + evt.error, "error");
            }
          } catch (parseErr) {
            // Skip malformed SSE events
          }
        }
      }
    } catch (e) {
      toast("Brain分析失败: " + e.message, "error");
    } finally {
      if (btn) {
        btn.disabled = false;
        btn.textContent = "开始分析";
      }
    }
  }

  function handleBrainDone(evt) {
    toast("✅ 分析完成", "success");

    // Update KPI status cards at top
    const outlookEl = $("brainOutlook");
    if (outlookEl && evt.market_outlook) {
      const info = OUTLOOK_MAP[evt.market_outlook];
      outlookEl.textContent = info ? `${info.icon} ${info.label}` : evt.market_outlook;
    }
    const confEl = $("brainConfidence");
    if (confEl && evt.confidence) confEl.textContent = `${evt.confidence}/100`;

    // ★ Render the full structured result panel
    renderBrainStructuredResult(evt);

    // Show actionable recommendations in morning orders panel
    if (evt.recommendations && evt.recommendations.length > 0) {
      renderBrainRecommendations(evt.recommendations);
    }

    // Reload state
    loadMorningOrders();
    loadBrainState();
  }

  /* ═══════════════════════════════════════════════════════════
     ★★★ STRUCTURED RESULT RENDERER ★★★
     Renders ALL structured data from the done event into a
     beautiful FinTech dashboard panel below the LLM markdown.
     ═══════════════════════════════════════════════════════════ */

  function renderBrainStructuredResult(data) {
    let container = document.getElementById("brainStructuredResult");
    if (!container) {
      // Create container after brainContent if it doesn't exist in DOM
      const contentEl = $("brainContent");
      if (!contentEl) return;
      container = document.createElement("div");
      container.id = "brainStructuredResult";
      contentEl.parentNode.insertBefore(container, contentEl.nextSibling);
    }

    const parts = [];

    // ── Section 1: Hero Summary Bar ──
    parts.push(_buildSummaryHero(data));

    // ── Section 2: Position Recommendations ──
    const recs = data.recommendations || data.position_recommendations || [];
    if (recs.length > 0) {
      parts.push(_buildPositionCards(recs));
    }

    // ── Section 3: Risk Factor Matrix ──
    const risks = data.risk_factors || [];
    if (risks.length > 0) {
      parts.push(_buildRiskMatrix(risks));
    }

    // ── Section 4: Strategy Updates ──
    const strategies = data.strategy_updates || [];
    if (strategies.length > 0) {
      parts.push(_buildStrategyUpdates(strategies));
    }

    // ── Section 5: Footer (next review + context) ──
    parts.push(_buildFooter(data));

    container.innerHTML = parts.join("");
    container.style.display = "block";

    // Animate entrance
    requestAnimationFrame(() => {
      container.classList.add("sr-visible");
    });
  }

  /* ── Hero Summary ── */
  function _buildSummaryHero(d) {
    const outlook = d.market_outlook || "neutral";
    const info = OUTLOOK_MAP[outlook] || { icon: "⚪", label: outlook, cls: "outlook-neut" };
    const conf = d.confidence || d.confidence_score || 0;
    const recs = d.recommendations || d.position_recommendations || [];
    const risks = d.risk_factors || [];
    const actionCount = recs.filter(r => r.action && r.action !== "hold").length;
    const holdCount = recs.filter(r => r.action === "hold").length;

    // Confidence ring color
    const confCls = conf >= 70 ? "conf-high" : conf >= 40 ? "conf-mid" : "conf-low";

    return `
    <div class="sr-hero">
      <div class="sr-hero-grid">
        <div class="sr-hero-card ${info.cls}">
          <div class="sr-hero-icon">${info.icon}</div>
          <div class="sr-hero-val">${info.label}</div>
          <div class="sr-hero-lbl">AI 怎么看市场</div>
        </div>
        <div class="sr-hero-card sr-hero-conf ${confCls}">
          <div class="sr-conf-ring" style="--conf-pct:${conf}">
            <svg viewBox="0 0 40 40" class="sr-ring-svg">
              <circle cx="20" cy="20" r="16" class="sr-ring-bg"/>
              <circle cx="20" cy="20" r="16" class="sr-ring-fg"
                      style="stroke-dasharray:${(conf / 100) * 100.5} 100.5"/>
            </svg>
            <span class="sr-conf-num">${conf}</span>
          </div>
          <div class="sr-hero-lbl">AI 多有把握</div>
        </div>
        <div class="sr-hero-card">
          <div class="sr-hero-icon">${actionCount > 0 ? "⚡" : "✓"}</div>
          <div class="sr-hero-val">${actionCount}<small class="sr-hero-sub">/ ${recs.length} 只</small></div>
          <div class="sr-hero-lbl">建议操作</div>
        </div>
        <div class="sr-hero-card">
          <div class="sr-hero-icon">${risks.length > 2 ? "⚠️" : risks.length > 0 ? "🛡️" : "✅"}</div>
          <div class="sr-hero-val">${risks.length}</div>
          <div class="sr-hero-lbl">需要注意的</div>
        </div>
      </div>
    </div>`;
  }

  /* ── Position Recommendation Cards ── */
  function _buildPositionCards(recs) {
    const cards = recs.map(r => {
      const act = ACTION_MAP[r.action] || ACTION_MAP.hold;
      const conf = r.confidence || 0;
      const confCls = conf >= 70 ? "conf-high" : conf >= 40 ? "conf-mid" : "conf-low";
      const hasAmount = r.amount && r.amount > 0;

      // Stop-loss / take-profit bar
      let slTpBar = "";
      if (r.stop_loss_pct || r.take_profit_pct) {
        slTpBar = `
          <div class="sr-pos-sltp">
            ${r.stop_loss_pct ? `<span class="sr-sl">止损 <b>${r.stop_loss_pct}%</b></span>` : ""}
            ${r.take_profit_pct ? `<span class="sr-tp">止盈 <b>${r.take_profit_pct}%</b></span>` : ""}
          </div>`;
      }

      return `
      <div class="sr-pos-card ${act.cls}">
        <div class="sr-pos-head">
          <span class="sr-pos-badge ${act.cls}">${act.icon} ${act.label}</span>
          <span class="sr-pos-symbol">${escHtml(r.symbol || "")}</span>
          <span class="sr-pos-name">${escHtml(r.asset_name || "")}</span>
          <span class="sr-pos-spacer"></span>
          ${hasAmount ? `<span class="sr-pos-amt">¥${fmtNum(r.amount)}</span>` : ""}
          <span class="sr-pos-conf ${confCls}" title="信心度 ${conf}%">
            <span class="sr-conf-bar-wrap">
              <span class="sr-conf-bar" style="width:${conf}%"></span>
            </span>
            ${conf}%
          </span>
        </div>
        ${slTpBar}
        ${r.reason ? `<div class="sr-pos-reason">${escHtml(r.reason)}</div>` : ""}
      </div>`;
    });

    return `
    <div class="sr-section">
      <div class="sr-section-head">
        <span class="sr-section-icon">📊</span>
        <span class="sr-section-title">AI 建议你这样做</span>
        <span class="sr-section-count">${recs.length} 项</span>
      </div>
      <div class="sr-pos-list">${cards.join("")}</div>
    </div>`;
  }

  /* ── Risk Factor Matrix ── */
  function _buildRiskMatrix(risks) {
    const rows = risks.map(r => {
      const p = (r.probability || "medium").toLowerCase();
      const i = (r.impact || "medium").toLowerCase();
      const key = `${p}-${i}`;
      const rc = RISK_COLOR[key] || RISK_COLOR["medium-medium"];

      return `
      <div class="sr-risk-row ${rc.cls}">
        <span class="sr-risk-dot"></span>
        <span class="sr-risk-factor">${escHtml(r.factor || "")}</span>
        <div class="sr-risk-tags">
          <span class="sr-risk-tag sr-risk-p" data-level="${p}">可能性 ${_riskLabel(p)}</span>
          <span class="sr-risk-tag sr-risk-i" data-level="${i}">影响 ${_riskLabel(i)}</span>
        </div>
      </div>`;
    });

    return `
    <div class="sr-section">
      <div class="sr-section-head">
        <span class="sr-section-icon">⚠️</span>
        <span class="sr-section-title">需要注意的事情</span>
        <span class="sr-section-count">${risks.length} 项</span>
      </div>
      <div class="sr-risk-list">${rows.join("")}</div>
    </div>`;
  }

  function _riskLabel(level) {
    return ({ high: "高", medium: "中", low: "低" })[level] || level;
  }

  /* ── Strategy Updates ── */
  function _buildStrategyUpdates(strategies) {
    const cards = strategies.map(s => {
      const isNew = s.action === "new";
      const actIcon = isNew ? "✦" : "↻";
      const actLabel = isNew ? "新发现" : "调整了";
      const actCls = isNew ? "strat-new" : "strat-update";

      return `
      <div class="sr-strat-card ${actCls}">
        <div class="sr-strat-head">
          <span class="sr-strat-badge ${actCls}">${actIcon} ${actLabel}</span>
          <span class="sr-strat-name">${escHtml(s.name || "")}</span>
        </div>
        ${s.logic ? `
          <div class="sr-strat-row">
            <span class="sr-strat-label">怎么做</span>
            <span class="sr-strat-text">${escHtml(s.logic)}</span>
          </div>` : ""}
        ${s.reason ? `
          <div class="sr-strat-row">
            <span class="sr-strat-label">为什么</span>
            <span class="sr-strat-text">${escHtml(s.reason)}</span>
          </div>` : ""}
      </div>`;
    });

    return `
    <div class="sr-section">
      <div class="sr-section-head">
        <span class="sr-section-icon">🎯</span>
        <span class="sr-section-title">AI 调整了什么</span>
        <span class="sr-section-count">${strategies.length} 项</span>
      </div>
      <div class="sr-strat-list">${cards.join("")}</div>
    </div>`;
  }

  /* ── Footer ── */
  function _buildFooter(d) {
    const nextReview = d.next_review || "";
    const ctx = d.context_summary || {};
    const cycleId = d.cycle_id || "";

    let contextChips = "";
    if (ctx.intel_count || ctx.holdings_count || ctx.cash) {
      contextChips = `
        <div class="sr-ctx-chips">
          ${ctx.intel_count ? `<span class="sr-ctx-chip">📰 AI 看了 ${ctx.intel_count} 条新闻</span>` : ""}
          ${ctx.holdings_count ? `<span class="sr-ctx-chip">📦 你持有 ${ctx.holdings_count} 只</span>` : ""}
          ${ctx.cash ? `<span class="sr-ctx-chip">💰 可用余额 ¥${fmtNum(ctx.cash)}</span>` : ""}
        </div>`;
    }

    return `
    <div class="sr-footer">
      ${contextChips}
      <div class="sr-footer-row">
        ${nextReview ? `<span class="sr-next-review">⏰ AI 下次分析时间: <b>${escHtml(nextReview)}</b></span>` : ""}
        ${cycleId ? `<span class="sr-cycle-id">${escHtml(cycleId)}</span>` : ""}
      </div>
    </div>`;
  }

  /* ═══════════════════════════════════════════════════════════ */

  function renderBrainRecommendations(recs) {
    const container = $("brainMorningOrders");
    const list = $("brainOrdersList");
    if (!container || !list) return;

    const actionRecs = recs.filter((r) => r.action && r.action !== "hold");
    if (actionRecs.length === 0) return;

    container.style.display = "block";
    list.innerHTML = actionRecs
      .map(
        (r) => `
      <div class="order-item ${r.action === "buy" || r.action === "add" ? "order-buy" : "order-sell"}">
        <div class="order-meta">
          <span class="order-action">${r.action === "buy" || r.action === "add" ? "🟢" : "🔴"} ${(ACTION_MAP[r.action] || {}).label || r.action}</span>
          <span class="order-symbol">${r.symbol || ""} ${escHtml(r.asset_name || "")}</span>
          <span class="order-amount">¥${fmtNum(r.amount || 0)}</span>
          <span class="order-confidence">信心: ${r.confidence || 0}%</span>
          ${r.stop_loss_pct ? `<span class="order-sl">亏这么多就卖: ${r.stop_loss_pct}%</span>` : ""}
          ${r.take_profit_pct ? `<span class="order-tp">赚这么多就卖: ${r.take_profit_pct}%</span>` : ""}
        </div>
        ${r.reason ? `<div class="order-reason">${escHtml(r.reason)}</div>` : ""}
      </div>`,
      )
      .join("");
  }

  function renderBrainCandidates(candidates) {
    // Screening data is AI-internal only — never show codes/scores/recs to users.
    // Just notify the user that AI found new opportunities.
    if (!candidates || !candidates.length) return;
    toast(`🔍 AI 发现了 ${candidates.length} 个新机会，已纳入分析`, "info");
  }

  function renderBrainAlerts(alerts) {
    if (!alerts || !alerts.length) return;
    toast(`⚡ ${alerts.length}条突发预警`, "warning");
  }

  // ═════ Brain Actions ═════

  async function toggleBrainAuto(enabled) {
    try {
      await api("/brain/auto/toggle", {
        method: "POST",
        body: JSON.stringify({ enabled }),
      });
      toast(enabled ? "自动分析已开启" : "自动分析已关闭", "success");
    } catch (e) {
      toast("切换失败: " + e.message, "error");
    }
  }

  async function viewBrainCycle(cycleId) {
    try {
      const data = await api(`/brain/cycles/${cycleId}`);
      const cycle = data.cycle;
      if (!cycle) return;

      // Render LLM markdown content
      const contentEl = $("brainContent");
      if (contentEl) {
        contentEl.innerHTML = F.renderMarkdown(cycle.analysis_content || "");
      }

      // ★ Also render the structured result if available
      const sr = cycle.structured_result;
      if (sr && typeof sr === "object" && Object.keys(sr).length > 0) {
        renderBrainStructuredResult(sr);
      }
    } catch (e) {
      toast("加载分析详情失败", "error");
    }
  }

  async function executeAllPending() {
    try {
      const data = await api("/trades?status=pending");
      const trades = data.trades || [];
      if (!trades.length) {
        toast("没有待执行的交易", "info");
        return;
      }
      const ids = trades.map((t) => t.id);
      await api("/trades/execute", {
        method: "POST",
        body: JSON.stringify({ trade_ids: ids }),
      });
      toast(`✅ 已执行 ${ids.length} 笔交易`, "success");
      loadMorningOrders();
      loadBrainState();
      if (F.loadHoldings) F.loadHoldings();
    } catch (e) {
      toast("执行失败: " + e.message, "error");
    }
  }

  async function dismissAllPending() {
    try {
      const data = await api("/trades?status=pending");
      const trades = data.trades || [];
      for (const t of trades) {
        await api(`/trades/${t.id}`, { method: "DELETE" });
      }
      toast("已全部暂缓", "info");
      loadMorningOrders();
    } catch (e) {
      toast("操作失败: " + e.message, "error");
    }
  }

  // ── Expose ──
  Object.assign(F, {
    loadBrainState,
    runBrainAnalysis,
    toggleBrainAuto,
    viewBrainCycle,
    executeAllPending,
    dismissAllPending,
    renderBrainStructuredResult,
  });
})(window.TradingApp);
