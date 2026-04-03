/* ═══════════════════════════════════════════════════════════
   trading/overview.js — Overview / Home page
   
   The first page users see. Designed for people with ZERO
   financial knowledge. Shows:
   1. Hero CTA → go try simulation
   2. AI's historical track record (sim results)
   3. AI's current market view
   4. Portfolio summary (if user has holdings)
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  'use strict';
  var api = F.api, toast = F.toast, fmtNum = F.fmtNum, fmtPct = F.fmtPct;
  var pnlClass = F.pnlClass, escHtml = F.escHtml;
  var $ = F._$;

  // ══════════════════════════════════════════
  //  Main entry
  // ══════════════════════════════════════════

  function loadOverview() {
    _loadTrackRecord();
    _loadAiView();
    _loadPortfolioPeek();
  }

  // ══════════════════════════════════════════
  //  AI Track Record — Simulation History
  // ══════════════════════════════════════════

  async function _loadTrackRecord() {
    try {
      var data = await api('/sim/sessions?limit=12');
      var sessions = data.sessions || [];
      var el = $('ovTrackRecord');
      if (!el) return;

      if (sessions.length === 0) {
        el.innerHTML =
          '<div class="ov-empty-track">' +
            '<div class="ov-empty-icon">🧪</div>' +
            '<p>还没有模拟记录</p>' +
            '<p class="ov-empty-sub">点击「免费模拟试试」，看看 AI 用过去真实行情能帮你赚多少钱</p>' +
            '<button class="btn ov-btn-cta-sm" onclick="TradingApp.navigate(\'simulator\')">开始第一次模拟</button>' +
          '</div>';
        return;
      }

      // ── Aggregate stats ──
      var totalSims = sessions.length;
      var completed = sessions.filter(function(s) { return s.status === 'completed'; });
      var profitable = completed.filter(function(s) { return (s.metrics || {}).total_return_pct > 0; });
      var profitRate = completed.length > 0 ? Math.round(profitable.length / completed.length * 100) : 0;
      var avgReturn = completed.length > 0
        ? completed.reduce(function(sum, s) { return sum + ((s.metrics || {}).total_return_pct || 0); }, 0) / completed.length
        : 0;
      var bestReturn = completed.reduce(function(best, s) {
        var r = (s.metrics || {}).total_return_pct || 0;
        return r > best ? r : best;
      }, -999);

      var statsHtml =
        '<div class="ov-track-stats">' +
          '<div class="ov-track-stat">' +
            '<div class="ov-track-stat-value">' + totalSims + '</div>' +
            '<div class="ov-track-stat-label">次模拟</div>' +
          '</div>' +
          '<div class="ov-track-stat">' +
            '<div class="ov-track-stat-value ' + (profitRate >= 50 ? 'up' : profitRate > 0 ? '' : 'down') + '">' + profitRate + '%</div>' +
            '<div class="ov-track-stat-label">赚钱比例</div>' +
          '</div>' +
          '<div class="ov-track-stat">' +
            '<div class="ov-track-stat-value ' + pnlClass(avgReturn) + '">' + (avgReturn >= 0 ? '+' : '') + fmtNum(avgReturn, 1) + '%</div>' +
            '<div class="ov-track-stat-label">平均收益</div>' +
          '</div>' +
          (bestReturn > -999 ? '<div class="ov-track-stat">' +
            '<div class="ov-track-stat-value up">' + (bestReturn >= 0 ? '+' : '') + fmtNum(bestReturn, 1) + '%</div>' +
            '<div class="ov-track-stat-label">最佳成绩</div>' +
          '</div>' : '') +
        '</div>';

      // ── Simulation cards ──
      var cardsHtml = sessions.slice(0, 6).map(function(s) {
        var m = s.metrics || {};
        var ret = m.total_return_pct || 0;
        var retSign = ret >= 0 ? '+' : '';
        var icon = ret >= 0 ? '📈' : '📉';
        var verdict = ret >= 0 ? '赚钱了！' : '这次亏了';
        var dd = m.max_drawdown_pct || 0;
        var wr = m.win_rate || 0;
        var _stratNames = {
          stable_income: '🏦 稳健理财', balanced: '⚖️ 均衡配置', growth: '🚀 积极成长',
          sector_rotation: '🔄 行业轮动', value: '💎 价值投资', freestyle: 'AI自由操盘',
          conservative: '🛡️ 保守型', aggressive: '🚀 进取型',
          auto: 'AI策略组合',
        };
        var stratLabel = _stratNames[s.strategy || s.risk_level || 'auto'] || 'AI策略组合';

        return '<div class="ov-track-card ' + pnlClass(ret) + '" onclick="TradingApp.navigate(\'simulator\');setTimeout(function(){TradingApp.viewSimSession(\'' + escHtml(s.session_id) + '\')},300)">' +
          '<div class="ov-track-card-header">' +
            '<span class="ov-track-card-period">' + escHtml(s.start_date || '') + ' → ' + escHtml(s.end_date || '') + '</span>' +
            '<span class="ov-track-card-verdict ' + pnlClass(ret) + '">' + icon + ' ' + verdict + '</span>' +
          '</div>' +
          '<div class="ov-track-card-return ' + pnlClass(ret) + '">' + retSign + fmtNum(ret, 2) + '%</div>' +
          '<div class="ov-track-card-details">' +
            '<span>中间最多亏 ' + fmtNum(dd, 1) + '%</span>' +
            '<span>每笔赚钱率 ' + fmtNum(wr, 0) + '%</span>' +
          '</div>' +
          '<div class="ov-track-card-risk">' + stratLabel + '</div>' +
        '</div>';
      }).join('');

      el.innerHTML = statsHtml + '<div class="ov-track-cards">' + cardsHtml + '</div>';

    } catch (e) {
      console.warn('[Overview] track record load failed', e);
    }
  }

  // ══════════════════════════════════════════
  //  AI Current Market View
  // ══════════════════════════════════════════

  async function _loadAiView() {
    try {
      var data = await api('/brain/state');
      var el = $('ovAiView');
      if (!el) return;

      var cycles = data.recent_cycles || [];
      if (cycles.length === 0) {
        el.innerHTML =
          '<div class="ov-ai-empty">' +
            '<p>AI 还没有分析过市场</p>' +
            '<button class="btn btn-outline btn-sm" onclick="TradingApp.navigate(\'brain\')">让 AI 分析一下 →</button>' +
          '</div>';
        return;
      }

      var latest = cycles[0];
      var outlook = latest.market_outlook || 'neutral';
      var outlookMap = {
        bullish:  { icon: '🟢', text: '看涨', desc: 'AI 觉得接下来市场可能会涨，可以考虑买入' },
        bearish:  { icon: '🔴', text: '看跌', desc: 'AI 觉得接下来市场风险较大，建议谨慎' },
        neutral:  { icon: '🟡', text: '观望', desc: 'AI 觉得目前市场方向还不明朗，建议等等再说' },
        cautious: { icon: '🟠', text: '谨慎', desc: 'AI 觉得目前有一些风险，建议小心操作' },
      };
      var info = outlookMap[outlook] || outlookMap.neutral;
      var conf = latest.confidence_score || 0;

      // Recommendations count
      var pendingCount = data.pending_trades || 0;
      var recStats = data.recommendation_stats || {};
      var totalRecs = (recStats.correct || 0) + (recStats.incorrect || 0);
      var winRate = totalRecs > 0 ? Math.round(recStats.correct / totalRecs * 100) : 0;

      el.innerHTML =
        '<div class="ov-ai-card-inner">' +
          '<div class="ov-ai-card-row">' +
            '<div class="ov-ai-outlook">' +
              '<div class="ov-ai-outlook-icon">' + info.icon + '</div>' +
              '<div class="ov-ai-outlook-text">' + info.text + '</div>' +
            '</div>' +
            '<div class="ov-ai-desc">' + info.desc + '</div>' +
            '<div class="ov-ai-conf">' +
              '<span class="ov-ai-conf-label">AI 把握度</span>' +
              '<span class="ov-ai-conf-value">' + conf + '%</span>' +
            '</div>' +
          '</div>' +
          (pendingCount > 0 ? '<div class="ov-ai-pending">📋 AI 有 <b>' + pendingCount + '</b> 个操作建议等你确认</div>' : '') +
          (winRate > 0 ? '<div class="ov-ai-winrate">历史建议准确率: <b>' + winRate + '%</b></div>' : '') +
          '<div class="ov-ai-footer">' +
            '<span class="ov-ai-time">上次分析: ' + escHtml(latest.created_at || '') + '</span>' +
            '<button class="btn btn-outline btn-sm" onclick="TradingApp.navigate(\'brain\')">查看详细分析 →</button>' +
          '</div>' +
        '</div>';

    } catch (e) {
      console.warn('[Overview] AI view load failed', e);
    }
  }

  // ══════════════════════════════════════════
  //  Portfolio Peek (simplified)
  // ══════════════════════════════════════════

  async function _loadPortfolioPeek() {
    try {
      var data = await api('/holdings');
      var holdings = data.holdings || [];
      var cash = data.available_cash || data.cash || 0;

      if (holdings.length === 0 && cash === 0) return;

      var section = $('ovPortfolioSection');
      if (section) section.style.display = '';

      var totalValue = holdings.reduce(function(s, h) {
        return s + (h.current_nav || h.buy_price) * h.shares;
      }, 0);
      var totalCost = holdings.reduce(function(s, h) {
        return s + h.buy_price * h.shares;
      }, 0);
      var pnl = totalValue - totalCost;
      var pnlPct = totalCost > 0 ? (pnl / totalCost) * 100 : 0;
      var pnlWord = pnl >= 0 ? '赚了 🎉' : '亏了 😔';

      var el = $('ovPortfolioPeek');
      if (!el) return;

      el.innerHTML =
        '<div class="ov-portfolio-row">' +
          '<div class="ov-portfolio-item ov-portfolio-primary">' +
            '<div class="ov-portfolio-label">我的总资产</div>' +
            '<div class="ov-portfolio-value">¥' + fmtNum(totalValue + cash) + '</div>' +
          '</div>' +
          '<div class="ov-portfolio-item">' +
            '<div class="ov-portfolio-label">' + pnlWord + '</div>' +
            '<div class="ov-portfolio-value ' + pnlClass(pnl) + '">' + (pnl >= 0 ? '+' : '') + '¥' + fmtNum(Math.abs(pnl)) + '</div>' +
            '<div class="ov-portfolio-sub ' + pnlClass(pnl) + '">' + (pnlPct >= 0 ? '+' : '') + fmtNum(pnlPct, 1) + '%</div>' +
          '</div>' +
          '<div class="ov-portfolio-item">' +
            '<div class="ov-portfolio-label">持有数量</div>' +
            '<div class="ov-portfolio-value">' + holdings.length + ' 只</div>' +
          '</div>' +
          '<div class="ov-portfolio-item">' +
            '<div class="ov-portfolio-label">可用余额</div>' +
            '<div class="ov-portfolio-value">¥' + fmtNum(cash) + '</div>' +
          '</div>' +
        '</div>';

    } catch (e) {
      console.warn('[Overview] portfolio peek load failed', e);
    }
  }

  // ── Expose ──
  F.loadOverview = loadOverview;
})(window.TradingApp);
