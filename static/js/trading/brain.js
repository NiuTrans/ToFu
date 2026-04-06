/* ═══════════════════════════════════════════════════════════
   trading/brain.js — Brain tab: unified AI decision center
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  "use strict";
  const { api, toast, fmtNum, fmtPct, pnlClass, escHtml, _$: $, _state: S } = F;

  let _lastResult = null;  // latest structured result for status row persistence

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
      toast("加载Brain状态失败: " + e.message, "error");
    }
  }

  /**
   * Populate the unified brain status row (4 cards: outlook, confidence ring,
   * pending/actions, risk count).
   * Called on page load from /brain/state and after analysis completes.
   */
  function renderBrainStatus(state) {
    const cycle = state.recent_cycles && state.recent_cycles[0] || null;

    // ── Card 1: Outlook ──
    _updateOutlookCard(cycle ? cycle.market_outlook : null);

    // ── Card 2: Confidence ring ──
    _updateConfidenceCard(cycle ? cycle.confidence_score : null);

    // ── Cards 3 & 4: use structured result data if available, else defaults ──
    if (_lastResult) {
      // Re-apply rich action/risk data from latest analysis (cards 3 & 4 only,
      // cards 1 & 2 already set from fresh API state above)
      _updateCardsFromResult(_lastResult);
    } else {
      // No analysis yet — show pending trades + risk defaults
      const pendingEl = $("brainPendingTrades");
      const actIconEl = $("brainActionsIcon");
      const actLabelEl = $("brainActionsLabel");
      if (pendingEl) pendingEl.textContent = state.pending_trades || 0;
      if (actIconEl) actIconEl.textContent = "📋";
      if (actLabelEl) actLabelEl.textContent = "等你确认";

      const riskEl = $("brainRiskCount");
      const riskIconEl = $("brainRiskIcon");
      if (riskEl) riskEl.textContent = "--";
      if (riskIconEl) riskIconEl.textContent = "✅";
    }

    // Show cycle info
    const infoEl = $("brainCycleInfo");
    if (infoEl && state.last_cycle) {
      infoEl.textContent = `上次分析: ${state.last_cycle}`;
    }
  }

  /** Update outlook card (icon, label, colored top-border class) */
  function _updateOutlookCard(outlook) {
    const el = $("brainOutlook");
    const iconEl = $("brainOutlookIcon");
    const card = $("brainCardOutlook");
    if (!el) return;

    const info = outlook ? OUTLOOK_MAP[outlook] : null;
    el.textContent = info ? info.label : (outlook || "--");
    if (iconEl) iconEl.textContent = info ? info.icon : "⚪";

    // Apply colored top-border class
    if (card) {
      card.className = "brain-stat-card";
      if (info) card.classList.add(info.cls);
    }
  }

  /** Update confidence SVG ring and number */
  function _updateConfidenceCard(score) {
    const numEl = $("brainConfidence");
    const arcEl = $("brainConfArc");
    const card = $("brainCardConf");
    const conf = typeof score === "number" ? score : 0;
    const hasData = typeof score === "number";

    if (numEl) numEl.textContent = hasData ? conf : "--";
    if (arcEl) {
      arcEl.style.strokeDasharray = hasData ? `${(conf / 100) * 100.5} 100.5` : "0 100.5";
    }
    // Color class on card
    if (card) {
      card.classList.remove("conf-high", "conf-mid", "conf-low");
      if (hasData) {
        card.classList.add(conf >= 70 ? "conf-high" : conf >= 40 ? "conf-mid" : "conf-low");
      }
    }
  }

  /** Update cards 3 & 4 only (actions + risks) — no outlook/confidence */
  function _updateCardsFromResult(data) {
    const recs = data.recommendations || data.position_recommendations || [];
    const risks = data.risk_factors || [];
    const actionCount = recs.filter(r => r.action && r.action !== "hold").length;

    // Card 3 — action count
    const actEl = $("brainPendingTrades");
    const actIconEl = $("brainActionsIcon");
    const actLabelEl = $("brainActionsLabel");
    if (actEl) {
      actEl.innerHTML = `${actionCount}<small class="brain-stat-sub">/ ${recs.length} 只</small>`;
    }
    if (actIconEl) actIconEl.textContent = actionCount > 0 ? "⚡" : "✓";
    if (actLabelEl) actLabelEl.textContent = "建议操作";

    // Card 4 — risk count
    const riskEl = $("brainRiskCount");
    const riskIconEl = $("brainRiskIcon");
    if (riskEl) riskEl.textContent = risks.length;
    if (riskIconEl) riskIconEl.textContent = risks.length > 2 ? "⚠️" : risks.length > 0 ? "🛡️" : "✅";
  }

  /** Update all 4 status cards from structured result data */
  function _updateStatusFromResult(data) {
    _lastResult = data;  // persist so renderBrainStatus won't overwrite cards 3/4
    _updateCardsFromResult(data);
    _updateOutlookCard(data.market_outlook);
    _updateConfidenceCard(data.confidence || data.confidence_score);
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
      const data = await api("/trades?status=pending");
      const container = $("brainMorningOrders");
      const list = $("brainOrdersList");
      if (!container || !list) return;

      const orders = data.trades || [];
      if (orders.length === 0) {
        container.style.display = "none";
        return;
      }

      container.style.display = "block";
      list.innerHTML = orders
        .map(
          (o) => `
        <div class="order-item ${o.action === "buy" || o.action === "add" ? "order-buy" : "order-sell"}">
          <div class="order-meta">
            <span class="order-action">${o.action === "buy" || o.action === "add" ? "🟢 买入" : "🔴 卖出"}</span>
            <span class="order-symbol">${o.symbol || ""} ${escHtml(o.asset_name || "")}</span>
            <span class="order-amount">¥${fmtNum(o.amount || 0)}</span>
          </div>
          ${o.reason ? `<div class="order-reason">${escHtml(o.reason)}</div>` : ""}
        </div>`,
        )
        .join("");
    } catch (e) {
      console.warn("[Brain] Failed to load pending trades:", e.message);
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
            console.debug("[Brain] Skipping malformed SSE event:", parseErr.message);
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
    if (evt.error && !evt.recommendations) {
      toast("分析出错: " + evt.error, "error");
      return;
    }
    toast(evt.storage_error ? "⚠️ 分析完成（保存异常）" : "✅ 分析完成", evt.storage_error ? "warning" : "success");

    // ★ Update unified status row immediately from done event
    _updateStatusFromResult(evt);

    // ★ Render structured result panel (without hero — hero is now the top row)
    renderBrainStructuredResult(evt);

    // Show actionable recommendations in morning orders panel
    if (evt.recommendations && evt.recommendations.length > 0) {
      renderBrainRecommendations(evt.recommendations);
    }

    // Reload state — renderBrainStatus will preserve cards 3/4 via _lastResult
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

    // (Hero summary removed — unified into top brain-status-row)

    // ── Section 1: Position Recommendations ──
    const recs = data.recommendations || data.position_recommendations || [];
    if (recs.length > 0) {
      parts.push(_buildPositionCards(recs));
    }

    // ── Section 2: Risk Factor Matrix ──
    const risks = data.risk_factors || [];
    if (risks.length > 0) {
      parts.push(_buildRiskMatrix(risks));
    }

    // ── Section 3: Strategy Updates ──
    const strategies = data.strategy_updates || [];
    if (strategies.length > 0) {
      parts.push(_buildStrategyUpdates(strategies));
    }

    // ── Section 4: Footer (next review + context) ──
    parts.push(_buildFooter(data));

    container.innerHTML = parts.join("");
    container.style.display = "block";

    // Animate entrance
    requestAnimationFrame(() => {
      container.classList.add("sr-visible");
    });
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

      // Asset type badge
      var typeBadge = '';
      var atype = (r.asset_type || '').toLowerCase();
      if (atype === 'stock' || atype === '股票') typeBadge = '<span class="sr-pos-type sr-type-stock">股票</span>';
      else if (atype === 'etf') typeBadge = '<span class="sr-pos-type sr-type-etf">ETF</span>';
      else if (atype === 'fund' || atype === '基金') typeBadge = '<span class="sr-pos-type sr-type-fund">基金</span>';

      return `
      <div class="sr-pos-card ${act.cls}">
        <div class="sr-pos-head">
          <span class="sr-pos-badge ${act.cls}">${act.icon} ${act.label}</span>
          ${typeBadge}
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
    var stocks = candidates.filter(function(c) { return c.asset_type === 'stock'; });
    var funds = candidates.filter(function(c) { return c.asset_type !== 'stock'; });
    var msg = '🔍 AI 发现了 ' + candidates.length + ' 个新机会';
    if (stocks.length > 0 && funds.length > 0) {
      msg += '（' + stocks.length + '只个股 + ' + funds.length + '只基金/ETF）';
    } else if (stocks.length > 0) {
      msg += '（' + stocks.length + '只个股）';
    } else if (funds.length > 0) {
      msg += '（' + funds.length + '只基金/ETF）';
    }
    msg += '，已纳入分析';
    toast(msg, "info");
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

      // ★ Update unified status row from this cycle's data
      const sr = cycle.structured_result;
      if (sr && typeof sr === "object" && Object.keys(sr).length > 0) {
        _updateStatusFromResult(sr);
        renderBrainStructuredResult(sr);
      } else {
        // Minimal update from cycle top-level fields
        _updateOutlookCard(cycle.market_outlook);
        _updateConfidenceCard(cycle.confidence_score);
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
