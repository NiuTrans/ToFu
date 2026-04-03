/* ═══════════════════════════════════════════════════════════
   trading/dashboard.js — Holdings, KPIs, search, cash management
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  "use strict";
  const { api, toast, fmtNum, fmtPct, pnlClass, escHtml, _$: $, _state: S } = F;

  // ═════ Holdings ═════

  async function loadHoldings() {
    try {
      const data = await api("/holdings");
      S.holdings = data.holdings || [];
      S.cash = data.available_cash || data.cash || 0;
      renderKPIs();
      renderHoldings();
    } catch (e) {
      toast("加载持仓失败: " + e.message, "error");
    }
  }

  function renderKPIs() {
    const totalValue = S.holdings.reduce(
      (s, h) => s + (h.current_nav || h.buy_price) * h.shares,
      0,
    );
    const totalCost = S.holdings.reduce(
      (s, h) => s + h.buy_price * h.shares,
      0,
    );
    const pnl = totalValue - totalCost;
    const pnlPct = totalCost > 0 ? (pnl / totalCost) * 100 : 0;

    // Estimated daily change (from est_nav)
    const estValue = S.holdings.reduce(
      (s, h) => s + (h.est_nav || h.current_nav || h.buy_price) * h.shares,
      0,
    );
    const dailyChange = estValue - totalValue;

    // Update individual KPI elements that exist in the HTML
    const elTotal = $("kpiTotal");
    if (elTotal) elTotal.textContent = "¥" + fmtNum(totalValue + S.cash);

    // Add profit/loss glow to the primary card
    const totalCard = elTotal?.closest?.(".kpi-card");
    if (totalCard) {
      totalCard.classList.remove("profit-glow", "loss-glow");
      if (pnl > 0) totalCard.classList.add("profit-glow");
      else if (pnl < 0) totalCard.classList.add("loss-glow");
    }

    const elTotalSub = $("kpiTotalSub");
    if (elTotalSub) {
      let subText = pnl >= 0 ? "↑ 正在赚钱" : "↓ 暂时亏了";
      // Append estimated daily change
      if (dailyChange !== 0 && S.holdings.some(h => h.est_nav)) {
        const sign = dailyChange > 0 ? "+" : "";
        subText += ` · 今天估计 ${sign}${fmtNum(dailyChange)}`;
      }
      elTotalSub.textContent = subText;
      elTotalSub.className = "kpi-sub " + pnlClass(pnl);
    }

    const elHolding = $("kpiHolding");
    if (elHolding) elHolding.textContent = "¥" + fmtNum(totalValue);

    const elCash = $("kpiCash");
    if (elCash) elCash.textContent = "¥" + fmtNum(S.cash);

    const elPnl = $("kpiPnl");
    if (elPnl) {
      elPnl.textContent = "¥" + fmtNum(pnl);
      elPnl.className = "kpi-value " + pnlClass(pnl);
    }

    const elPnlPct = $("kpiPnlPct");
    if (elPnlPct) {
      elPnlPct.textContent = fmtPct(pnlPct);
      elPnlPct.className = "kpi-sub " + pnlClass(pnl);
    }

    // Estimated daily change KPI card (matches #kpiEstChange in HTML)
    const elEstChg = $("kpiEstChange");
    if (elEstChg) {
      if (S.holdings.some(h => h.est_nav)) {
        elEstChg.textContent = (dailyChange >= 0 ? "+" : "") + "¥" + fmtNum(dailyChange);
        elEstChg.className = "kpi-value " + pnlClass(dailyChange);
      } else {
        elEstChg.textContent = "--";
        elEstChg.className = "kpi-value";
      }
    }

    const elCount = $("kpiCount");
    if (elCount) {
      elCount.textContent = S.holdings.length > 0 ? S.holdings.length + " 只" : "0 只";
    }
  }

  function renderHoldings() {
    const tbody = $("holdingsBody");
    const empty = $("holdingsEmpty");
    if (!tbody) return;

    if (S.holdings.length === 0) {
      tbody.innerHTML = "";
      if (empty) empty.style.display = "flex";
      return;
    }
    if (empty) empty.style.display = "none";

    tbody.innerHTML = S.holdings
      .map((h) => {
        const nav = h.current_nav || h.buy_price;
        const marketValue = nav * h.shares;
        const holdingPnl = (nav - h.buy_price) * h.shares;
        const holdingPnlPct =
          h.buy_price > 0 ? ((nav - h.buy_price) / h.buy_price) * 100 : 0;
        const rowCls = h.current_nav ? (holdingPnl > 0 ? 'row-profit' : holdingPnl < 0 ? 'row-loss' : '') : '';
        return `
      <tr class="${rowCls}">
        <td class="code">${h.symbol}</td>
        <td>${escHtml(h.asset_name || "--")}</td>
        <td class="num">${fmtNum(h.shares, 2)}</td>
        <td class="num">¥${fmtNum(h.buy_price, 4)}</td>
        <td class="num">${h.current_nav ? "¥" + fmtNum(h.current_nav, 4) : "--"}${h.nav_date ? `<small class="nav-date">${h.nav_date}</small>` : ""}</td>
        <td class="num">${h.est_nav ? "¥" + fmtNum(h.est_nav, 4) : "--"}</td>
        <td class="num">¥${fmtNum(marketValue)}</td>
        <td class="num ${pnlClass(holdingPnl)}">
          ${h.current_nav ? (holdingPnl >= 0 ? "赚了 " : "亏了 ") + "¥" + fmtNum(Math.abs(holdingPnl)) : "--"}
        </td>
        <td class="num ${pnlClass(holdingPnl)}">
          ${h.current_nav ? fmtPct(holdingPnlPct) : "--"}
        </td>
        <td>
          <button class="btn btn-xs btn-outline" onclick="TradingApp.editHolding(${h.id})">编辑</button>
          <button class="btn btn-xs btn-danger-outline" onclick="TradingApp.deleteHolding(${h.id})">删除</button>
        </td>
      </tr>`;
      })
      .join("");
  }

  // ═════ Add / Edit / Delete Holdings ═════

  function showAddHolding() {
    const modal = $("addHoldingModal");
    if (modal) modal.style.display = "flex";
    $("searchFundInput")?.focus();
  }

  let _searchTimer = null;
  async function _searchFund(q) {
    clearTimeout(_searchTimer);
    if (!q || q.length < 2) {
      const r = $("searchFundResults");
      if (r) r.innerHTML = "";
      return;
    }
    _searchTimer = setTimeout(async () => {
      try {
        const data = await api(`/search?q=${encodeURIComponent(q)}`);
        const list = data.results || [];
        const r = $("searchFundResults");
        if (r)
          r.innerHTML =
            list
              .map(
                (f) =>
                  `<div class="search-result-item" onclick="TradingApp._selectFund('${escHtml(f.code)}','${escHtml(f.name)}')">${f.code} — ${escHtml(f.name)}</div>`,
              )
              .join("") || '<div class="search-empty">未找到标的</div>';
      } catch {
        const r = $("searchFundResults");
        if (r) r.innerHTML = "";
      }
    }, 300);
  }

  function _selectFund(code, name) {
    const codeEl = $("addFundCode");
    if (codeEl) codeEl.value = code;
    const searchEl = $("searchFundInput");
    if (searchEl) searchEl.value = `${code} — ${name}`;
    const r = $("searchFundResults");
    if (r) r.innerHTML = "";
  }

  async function addHolding() {
    const code = $("addFundCode")?.value?.trim();
    const shares = parseFloat($("addShares")?.value);
    const price = parseFloat($("addBuyPrice")?.value);
    const date = $("addBuyDate")?.value;
    const note = $("addNote")?.value?.trim();
    if (!code || !shares || !price) {
      toast("请填写完整信息", "error");
      return;
    }
    try {
      await api("/holdings", {
        method: "POST",
        body: JSON.stringify({
          symbol: code,
          asset_name: "",
          shares,
          buy_price: price,
          buy_date: date || "",
          note: note || "",
        }),
      });
      toast("添加成功", "success");
      const modal = $("addHoldingModal");
      if (modal) modal.style.display = "none";
      // Reset form
      [
        "searchFundInput",
        "addFundCode",
        "addShares",
        "addBuyPrice",
        "addBuyDate",
        "addNote",
      ].forEach((id) => {
        const el = $(id);
        if (el) el.value = "";
      });
      loadHoldings();
    } catch (e) {
      toast("添加失败: " + e.message, "error");
    }
  }

  async function editHolding(id) {
    const h = S.holdings.find((x) => x.id === id);
    if (!h) return;
    const newShares = prompt("修改份额:", h.shares);
    if (newShares === null) return;
    const newPrice = prompt("修改成本价:", h.buy_price);
    if (newPrice === null) return;
    try {
      await api(`/holdings/${id}`, {
        method: "PUT",
        body: JSON.stringify({
          shares: parseFloat(newShares),
          buy_price: parseFloat(newPrice),
        }),
      });
      toast("更新成功", "success");
      loadHoldings();
    } catch (e) {
      toast("更新失败: " + e.message, "error");
    }
  }

  async function deleteHolding(id) {
    if (!confirm("确定删除该持仓？")) return;
    try {
      await api(`/holdings/${id}`, { method: "DELETE" });
      toast("已删除", "success");
      loadHoldings();
    } catch (e) {
      toast("删除失败: " + e.message, "error");
    }
  }

  async function deleteAllHoldings() {
    if (S.holdings.length === 0) {
      toast("你还没有持有任何投资", "info");
      return;
    }
    if (!confirm(`确定要清空全部 ${S.holdings.length} 只持仓吗？此操作不可撤销。`)) return;
    try {
      const data = await api("/holdings/all", { method: "DELETE" });
      toast(`已清仓 ${data.deleted || S.holdings.length} 条持仓`, "success");
      loadHoldings();
    } catch (e) {
      toast("一键清仓失败: " + e.message, "error");
    }
  }

  // ═════ Cash ═════

  function showCashModal() {
    const modal = $("cashModal");
    if (modal) modal.style.display = "flex";
    const el = $("cashInput");
    if (el) el.value = S.cash;
  }

  async function saveCash() {
    const val = parseFloat($("cashInput")?.value);
    if (isNaN(val)) {
      toast("请输入有效金额", "error");
      return;
    }
    try {
      await api("/cash", {
        method: "POST",
        body: JSON.stringify({ amount: val }),
      });
      toast("现金已更新", "success");
      const modal = $("cashModal");
      if (modal) modal.style.display = "none";
      loadHoldings();
    } catch (e) {
      toast("保存失败: " + e.message, "error");
    }
  }

  // ═════ Trade History (merged from legacy History page) ═════

  async function loadHistory() {
    try {
      const data = await api("/trades?limit=100");
      const trades = data.trades || [];
      const tbody = $("dashTradeHistoryBody");
      if (!tbody) return;
      if (trades.length === 0) {
        tbody.innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--t3);padding:16px">还没有买卖记录</td></tr>';
        return;
      }
      tbody.innerHTML = trades.map(t => {
        const actionLabel = t.action === 'buy' ? '🟢 买入' : t.action === 'sell' ? '🔴 卖出' : t.action || '--';
        return `<tr>
          <td>${escHtml(t.date || t.created_at || '--')}</td>
          <td>${escHtml(t.asset_name || t.symbol || '--')}</td>
          <td>${actionLabel}</td>
          <td class="num">${t.shares ? fmtNum(t.shares, 2) : '--'}</td>
          <td class="num">${t.price ? '¥' + fmtNum(t.price, 4) : '--'}</td>
          <td class="num">${t.amount ? '¥' + fmtNum(t.amount) : '--'}</td>
          <td>${escHtml(t.note || t.reason || '--')}</td>
        </tr>`;
      }).join('');
    } catch (e) {
      // Non-critical - just show empty
    }
  }

  function exportCSV() {
    const tbody = $("dashTradeHistoryBody");
    const rows = tbody ? tbody.querySelectorAll("tr") : [];
    if (rows.length === 0) return toast("没有数据可导出", "error");
    const header = ["日期", "名称", "买/卖", "数量", "价格", "金额", "备注"];
    const lines = [header.join(",")];
    rows.forEach(function (r) {
      const cells = Array.from(r.cells).map(function (c) {
        return '"' + c.textContent.trim() + '"';
      });
      lines.push(cells.join(","));
    });
    const blob = new Blob(["\uFEFF" + lines.join("\n")], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "trading_history_" + new Date().toISOString().slice(0, 10) + ".csv";
    a.click();
    URL.revokeObjectURL(url);
    toast("导出成功", "success");
  }

  // ═════ Auto-refresh ═════

  function startRefresh() {
    if (S.refreshTimer) return;
    S.refreshTimer = setInterval(async () => {
      if (S.currentPage === "dashboard") {
        try {
          const data = await api("/holdings");
          S.holdings = data.holdings || [];
          renderKPIs();
          renderHoldings();
        } catch {}
      }
    }, 60000);
  }

  function stopRefresh() {
    if (S.refreshTimer) {
      clearInterval(S.refreshTimer);
      S.refreshTimer = null;
    }
  }

  // ── Expose ──
  Object.assign(F, {
    loadHoldings,
    showAddHolding,
    addHolding,
    editHolding,
    deleteHolding,
    deleteAllHoldings,
    showCashModal,
    saveCash,
    _searchFund,
    _selectFund,
    startRefresh,
    stopRefresh,
    // Internal (used by other modules)
    loadHistory,
    exportCSV,
    _renderKPIs: renderKPIs,
    _renderHoldings: renderHoldings,
  });
})(window.TradingApp);
