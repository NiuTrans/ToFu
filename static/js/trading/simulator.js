/* ═══════════════════════════════════════════════════════════
   trading/simulator.js — Unified LLM Historical Simulator UI

   Simplified flow: user picks TIME + RISK LEVEL + CAPITAL
   AI automatically chooses which funds to buy/sell.
   No financial knowledge required.

   ★ FIX: Full browser refresh persistence —
      Both fetch-data and sim-run use server-side POLLING.
      task_id + state are saved in sessionStorage so a full F5 refresh
      can resume from cursor=0, replaying ALL events from the server.
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  'use strict';
  var api = F.api, toast = F.toast, fmtNum = F.fmtNum, fmtPct = F.fmtPct;
  var pnlClass = F.pnlClass, escHtml = F.escHtml, renderMarkdown = F.renderMarkdown;
  var $ = F._$, S = F._state;

  // ── Session storage keys ──
  var SK_STATE      = 'sim_state';          // 'setup' | 'fetch' | 'run' | 'results'
  var SK_FETCH_TASK = 'sim_fetch_task_id';
  var SK_SIM_TASK   = 'sim_run_task_id';
  var SK_PARAMS     = 'sim_params';         // JSON {startDate, endDate, riskLevel, symbols, capital, ...}

  // ── Persistent State (survives page switches AND browser refresh) ──
  var _simState = sessionStorage.getItem(SK_STATE) || 'setup';
  var _sessions = [];
  var _activeSessionId = null;
  var _strategyId = 'auto';  // LLM picks strategies autonomously
  var _benchmarkIndex = '1.000300';
  var BENCHMARK_NAMES = {
    '1.000300': '沪深300', '1.000001': '上证指数', '0.399001': '深证成指',
    '0.399006': '创业板指', '1.000905': '中证500', '1.000852': '中证1000',
  };

  // ── Background stream tracking (survives page switches, rebuilt on refresh) ──
  var _activeFetchPoll = null;     // setInterval ID for fetch polling
  var _activeSimPoll   = null;     // setInterval ID for sim polling
  var _fetchLogHistory = [];       // accumulated log lines
  var _fetchPhasesDone = {};       // which fetch phases are done
  var _fetchPhaseProgress = {};    // last {done, total, msg} per phase
  var _fetchComplete = false;      // fetch finished?
  var _simComplete = false;        // simulation finished?
  var _simDecisionCount = 0;
  var _simEquityPoints = [];
  var _simTimelineEntries = [];    // ★ persist timeline for rebuild on refresh
  var _simTradeEntries = [];       // ★ persist trade sub-entries
  var _simInitialCapital = 100000;
  var _simResultData = null;       // final result once complete
  var _fetchElapsedTimer = null;   // elapsed time display timer
  var _fetchStartTime = null;

  // ── Asset name lookup — dynamically expanded when user searches & adds ──
  var FUND_NAMES = {
    '510300': '华泰柏瑞沪深300ETF',
    '510500': '南方中证500ETF',
    '159915': '易方达创业板ETF',
    '510050': '华夏上证50ETF',
    '512100': '南方中证1000ETF',
    '512880': '国泰中证全指证券公司ETF',
    '512010': '易方达沪深300医药卫生ETF',
    '515790': '华泰柏瑞中证光伏产业ETF',
    '159825': '天弘中证农业主题ETF',
    '512690': '鹏华中证酒ETF',
    '511010': '国泰上证5年期国债ETF',
    '511260': '国泰上证10年期国债ETF',
    '511220': '海富通上证城投债ETF',
    '159972': '华夏中证海外中国互联网50ETF',
    '513500': '博时标普500ETF',
  };
  var FUND_ICONS = {
    '510300': '🔵', '510500': '🟣', '159915': '🟢', '510050': '🔴', '512100': '🟡',
    '512880': '📈', '512010': '💊', '515790': '☀️', '159825': '🌾', '512690': '🍷',
    '511010': '🏛️', '511260': '📜', '511220': '🏗️', '159972': '🌐', '513500': '🇺🇸',
  };

  // Type → default icon for assets not in FUND_ICONS
  var TYPE_ICONS = {
    '股票': '📊', 'ETF': '📦', '基金': '💼', '债券': '📜', '其他': '📄',
  };

  // ── Strategy Lab — loaded dynamically from DB ──
  var _strategyLabData = null;  // cached from /api/trading/sim/strategies
  var _strategyLabLoading = false;
  var _TYPE_ICONS = {
    'buy_signal':   '📈', 'sell_signal':  '📉',
    'risk_control': '🛡️', 'allocation':   '⚖️',
    'timing':       '⏰', 'observation':  '👁️',
  };

  // ── Quick-add symbol groups (not tied to any strategy) ──
  var QUICK_ADD_GROUPS = {
    broad_index: {
      label: '📊 宽基指数', items: [
        {code:'510300',name:'华泰柏瑞沪深300ETF',type:'ETF'},
        {code:'510500',name:'南方中证500ETF',type:'ETF'},
        {code:'159915',name:'易方达创业板ETF',type:'ETF'},
        {code:'510050',name:'华夏上证50ETF',type:'ETF'},
        {code:'512100',name:'南方中证1000ETF',type:'ETF'},
      ]
    },
    sector: {
      label: '🏭 行业精选', items: [
        {code:'512880',name:'国泰中证全指证券公司ETF',type:'ETF'},
        {code:'512010',name:'易方达沪深300医药卫生ETF',type:'ETF'},
        {code:'515790',name:'华泰柏瑞中证光伏产业ETF',type:'ETF'},
        {code:'512690',name:'鹏华中证酒ETF',type:'ETF'},
        {code:'159825',name:'天弘中证农业主题ETF',type:'ETF'},
      ]
    },
    bond: {
      label: '🏛️ 债券固收', items: [
        {code:'511010',name:'国泰上证5年期国债ETF',type:'ETF'},
        {code:'511260',name:'国泰上证10年期国债ETF',type:'ETF'},
        {code:'511220',name:'海富通上证城投债ETF',type:'ETF'},
      ]
    },
    cross_border: {
      label: '🌐 跨境配置', items: [
        {code:'159972',name:'华夏中证海外中国互联网50ETF',type:'ETF'},
        {code:'513500',name:'博时标普500ETF',type:'ETF'},
      ]
    },
    blue_chip: {
      label: '🏢 大白马股票', items: [
        {code:'600519',name:'贵州茅台',type:'股票'},
        {code:'000858',name:'五粮液',type:'股票'},
        {code:'601318',name:'中国平安',type:'股票'},
        {code:'600036',name:'招商银行',type:'股票'},
        {code:'000333',name:'美的集团',type:'股票'},
      ]
    },
  };

  // ── Custom symbol list (user-editable, survives within session) ──
  var _customSymbols = [];   // [{code, name, type}]
  var _searchTimer = null;

  // Phase ID mapping
  var PHASE_IDS = {
    setup:   'simSetupPhase',
    fetch:   'simFetchPhase',
    run:     'simRunPhase',
    results: 'simResultsPhase'
  };

  // ══════════════════════════════════════════
  //  SessionStorage helpers
  // ══════════════════════════════════════════

  function _saveState(state) {
    _simState = state;
    sessionStorage.setItem(SK_STATE, state);
  }

  function _saveParams(params) {
    try { sessionStorage.setItem(SK_PARAMS, JSON.stringify(params)); } catch (e) { /* ok */ }
  }

  function _loadParams() {
    try { return JSON.parse(sessionStorage.getItem(SK_PARAMS) || '{}'); } catch (e) { return {}; }
  }

  function _clearSession() {
    sessionStorage.removeItem(SK_STATE);
    sessionStorage.removeItem(SK_FETCH_TASK);
    sessionStorage.removeItem(SK_SIM_TASK);
    sessionStorage.removeItem(SK_PARAMS);
  }

  // ══════════════════════════════════════════
  //  Initialize
  // ══════════════════════════════════════════

  function loadSimulator() {
    _prefillDates();
    _loadStrategyLab();
    _renderSelectedSymbols();
    _loadSessions();

    // ★ Restore state from sessionStorage after full browser refresh
    var savedState = sessionStorage.getItem(SK_STATE) || 'setup';
    _simState = savedState;

    if (savedState === 'fetch') {
      _showPhase('fetch');
      var fetchTaskId = sessionStorage.getItem(SK_FETCH_TASK);
      if (fetchTaskId && !_activeFetchPoll) {
        // ★ Resume polling from cursor=0 — server has all events in memory
        _resumeFetchPoll(fetchTaskId);
      } else {
        // Just replay accumulated logs from this session (SPA switch, not refresh)
        _replayFetchLogs();
        if (_fetchComplete) {
          var btnEl = $('simProceedToRun');
          if (btnEl) btnEl.style.display = 'inline-flex';
        }
      }
    } else if (savedState === 'run') {
      _showPhase('run');
      var simTaskId = sessionStorage.getItem(SK_SIM_TASK);
      if (simTaskId && !_activeSimPoll) {
        // ★ Resume polling from cursor=0 — rebuilds timeline from replayed events
        _resumeSimPoll(simTaskId);
      } else {
        _restoreSimRunUI();
      }
    } else if (savedState === 'results' && _simResultData) {
      _showPhase('results');
      _showResults(_simResultData);
    } else if (savedState === 'results') {
      // Results phase but data lost (refresh) — try loading from saved params
      var params = _loadParams();
      _showPhase('results');
      // If we have a session_id, load from server
      if (_activeSessionId) {
        viewSimSession(_activeSessionId);
      } else {
        // No session_id — go back to setup
        _saveState('setup');
        _showPhase('setup');
      }
    } else {
      _showPhase('setup');
    }
  }

  function _prefillDates() {
    var now = new Date();
    var end = _fmtDate(now);
    var start = _fmtDate(new Date(now.getTime() - 180 * 86400000));
    var startEl = $('simStartDate');
    var endEl = $('simEndDate');
    // Restore dates from params if available
    var params = _loadParams();
    if (params.startDate && startEl) startEl.value = params.startDate;
    else if (startEl && !startEl.value) startEl.value = start;
    if (params.endDate && endEl) endEl.value = params.endDate;
    else if (endEl && !endEl.value) endEl.value = end;
    // Restore strategy (backward compat — now always 'auto')
    _strategyId = params.strategyId || params.riskLevel || 'auto';
    // Restore benchmark index
    if (params.benchmarkIndex) {
      _benchmarkIndex = params.benchmarkIndex;
      var benchSel = $('simBenchmarkIndex');
      if (benchSel) benchSel.value = _benchmarkIndex;
    }
    if (params.capital) {
      var capEl = $('simCapital');
      if (capEl) capEl.value = params.capital;
    }
  }

  function _fmtDate(d) {
    return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
  }

  // ══════════════════════════════════════════
  //  Strategy Selection (observable investment style)
  // ══════════════════════════════════════════

  function selectBenchmark(secid) {
    _benchmarkIndex = secid;
  }

  // ══════════════════════════════════════════
  //  Strategy Lab — dynamic from DB
  // ══════════════════════════════════════════

  function _loadStrategyLab() {
    var container = $('simStrategyLab');
    if (!container) return;
    if (_strategyLabLoading) return;
    _strategyLabLoading = true;
    container.innerHTML = '<div class="sim-slab-loading"><div class="sim-slab-spinner"></div> 正在加载策略数据...</div>';

    api('/sim/strategies').then(function (data) {
      _strategyLabData = data;
      _strategyLabLoading = false;
      _renderStrategyLab(data);
    }).catch(function (e) {
      _strategyLabLoading = false;
      container.innerHTML = '<div class="sim-slab-empty">策略数据加载失败，AI 将使用默认策略</div>';
      console.error('[Sim] Strategy lab load failed:', e);
    });
  }

  function _renderStrategyLab(data) {
    var container = $('simStrategyLab');
    if (!container) return;

    var strats = data.strategies || [];
    var perf = data.performance || {};
    var agg = data.aggregate || {};
    var typeLabels = data.type_labels || {};
    var activeStrats = strats.filter(function (s) { return s.status === 'active'; });

    if (activeStrats.length === 0) {
      container.innerHTML = '<div class="sim-slab-empty">策略库为空。请先在投资大脑中添加策略，AI 才能学习和进化。</div>';
      return;
    }

    // ── Aggregate stats bar ──
    var withData = 0;
    var totalUses = 0;
    var bestStrat = null, bestWR = -1;
    activeStrats.forEach(function (s) {
      var p = perf[s.id];
      if (p && p.total_uses > 0) {
        withData++;
        totalUses += p.total_uses;
        if (p.win_rate > bestWR) { bestWR = p.win_rate; bestStrat = s; }
      }
    });

    var statsHtml = '<div class="sim-slab-stats">' +
      '<div class="sim-slab-stat"><div class="sim-slab-stat-val">' + activeStrats.length + '</div><div class="sim-slab-stat-label">可用策略</div></div>' +
      '<div class="sim-slab-stat"><div class="sim-slab-stat-val">' + totalUses + '</div><div class="sim-slab-stat-label">总使用次数</div></div>' +
      (bestStrat ? '<div class="sim-slab-stat"><div class="sim-slab-stat-val sim-slab-best">' + escHtml(bestStrat.name) + '</div><div class="sim-slab-stat-label">🏆 最高胜率 ' + fmtNum(bestWR, 0) + '%</div></div>' : '') +
      (agg.avg_win_rate != null ? '<div class="sim-slab-stat"><div class="sim-slab-stat-val ' + (agg.avg_win_rate >= 50 ? 'up' : '') + '">' + fmtNum(agg.avg_win_rate, 1) + '%</div><div class="sim-slab-stat-label">平均胜率</div></div>' : '') +
    '</div>';

    // ── Strategy cards by type ──
    var byType = {};
    activeStrats.forEach(function (s) {
      var t = s.type || 'observation';
      if (!byType[t]) byType[t] = [];
      byType[t].push(s);
    });

    var typeOrder = ['risk_control', 'buy_signal', 'sell_signal', 'allocation', 'timing', 'observation'];
    var cardsHtml = '';
    typeOrder.forEach(function (type) {
      var items = byType[type];
      if (!items || items.length === 0) return;
      var typeIcon = _TYPE_ICONS[type] || '📋';
      var typeLabel = typeLabels[type] || type;
      cardsHtml += '<div class="sim-slab-type-group">' +
        '<div class="sim-slab-type-header">' + typeLabel + '</div>' +
        '<div class="sim-slab-type-cards">';
      items.forEach(function (s) {
        var p = perf[s.id] || {};
        var wr = p.win_rate;
        var wrHtml = '', usesHtml = '', avgHtml = '';
        if (p.total_uses > 0) {
          var wrCls = wr >= 60 ? 'sim-slab-wr-good' : wr >= 40 ? 'sim-slab-wr-mid' : 'sim-slab-wr-bad';
          wrHtml = '<div class="sim-slab-card-wr ' + wrCls + '">' +
            '<span class="sim-slab-wr-num">' + fmtNum(wr, 0) + '%</span>' +
            '<span class="sim-slab-wr-label">胜率</span></div>';
          usesHtml = '<span class="sim-slab-card-uses">' + p.total_uses + '次</span>';
          avgHtml = p.avg_return != null
            ? '<span class="sim-slab-card-avg ' + (p.avg_return >= 0 ? 'up' : 'down') + '">' +
              (p.avg_return >= 0 ? '+' : '') + fmtNum(p.avg_return, 1) + '%</span>'
            : '';
        } else {
          wrHtml = '<div class="sim-slab-card-wr sim-slab-wr-new"><span class="sim-slab-wr-num">NEW</span><span class="sim-slab-wr-label">待验证</span></div>';
        }

        cardsHtml += '<div class="sim-slab-card">' +
          wrHtml +
          '<div class="sim-slab-card-name">' + escHtml(s.name) + '</div>' +
          '<div class="sim-slab-card-logic">' + escHtml((s.logic || '').substring(0, 100)) + '</div>' +
          '<div class="sim-slab-card-footer">' +
            '<span class="sim-slab-card-source">' + (s.source === 'evolved' ? '🧬 进化' : s.source === 'learned' ? '📚 学习' : '✍️ 人工') + '</span>' +
            usesHtml + avgHtml +
          '</div>' +
        '</div>';
      });
      cardsHtml += '</div></div>';
    });

    container.innerHTML = statsHtml +
      '<div class="sim-slab-cards-wrap">' + cardsHtml + '</div>' +
      '<div class="sim-slab-footer">' +
        '<div class="sim-slab-footer-text">💡 AI 会在每次决策中自主选择最合适的策略组合。模拟结束后，策略的胜率会自动更新。</div>' +
      '</div>';
  }

  function _getSymbolList() {
    return _customSymbols.map(function (s) { return s.code; });
  }

  // ── Quick-add group ──
  function addQuickGroup(groupKey) {
    var group = QUICK_ADD_GROUPS[groupKey];
    if (!group) return;
    var added = 0;
    group.items.forEach(function (item) {
      var already = _customSymbols.some(function (s) { return s.code === item.code; });
      if (!already) {
        _customSymbols.push({ code: item.code, name: item.name, type: item.type });
        if (item.name) FUND_NAMES[item.code] = item.name;
        added++;
      }
    });
    _renderSelectedSymbols();
    if (added > 0) toast('已添加 ' + added + ' 只' + group.label.replace(/^[^\s]+\s/, ''), 'success');
    else toast('这组标的都已经在列表中了', 'info');
  }

  // ══════════════════════════════════════════
  //  Custom Symbol Search & Management
  // ══════════════════════════════════════════

  function _getSymbolNames() {
    var names = {};
    _customSymbols.forEach(function (s) {
      if (s.name && s.name !== s.code) names[s.code] = s.name;
    });
    return names;
  }

  function addSymbol(code, name, type) {
    // Prevent duplicates
    for (var i = 0; i < _customSymbols.length; i++) {
      if (_customSymbols[i].code === code) {
        toast('已在列表中: ' + (name || code), 'warn');
        return;
      }
    }
    _customSymbols.push({ code: code, name: name || code, type: type || '' });
    // Register name in FUND_NAMES for display everywhere
    if (name) FUND_NAMES[code] = name;
    _renderSelectedSymbols();
    // Clear search
    var searchInput = $('simSymbolSearch');
    if (searchInput) searchInput.value = '';
    var resultsEl = $('simSearchResults');
    if (resultsEl) resultsEl.innerHTML = '';
  }

  function removeSymbol(code) {
    _customSymbols = _customSymbols.filter(function (s) { return s.code !== code; });
    _renderSelectedSymbols();
  }

  function clearAllSymbols() {
    _customSymbols = [];
    _renderSelectedSymbols();
  }

  function _renderSelectedSymbols() {
    var container = $('simSelectedSymbols');
    if (!container) return;
    if (_customSymbols.length === 0) {
      container.innerHTML = '<div class="sim-symbols-empty">请通过上方快捷按钮或搜索添加投资标的</div>';
      return;
    }
    var html = '';
    _customSymbols.forEach(function (s) {
      var icon = FUND_ICONS[s.code] || TYPE_ICONS[s.type] || '📄';
      var typeTag = s.type ? '<span class="sim-sym-type">' + escHtml(s.type) + '</span>' : '';
      html += '<div class="sim-symbol-tag">'
        + '<span class="sim-sym-icon">' + icon + '</span>'
        + '<span class="sim-sym-name">' + escHtml(s.name || s.code) + '</span>'
        + '<span class="sim-sym-code">' + escHtml(s.code) + '</span>'
        + typeTag
        + '<button class="sim-sym-remove" onclick="TradingApp.removeSymbol(\'' + s.code + '\')" title="移除">✕</button>'
        + '</div>';
    });
    container.innerHTML = html;
    // Update count display
    var countEl = $('simSymbolCount');
    if (countEl) countEl.textContent = _customSymbols.length + ' 只标的';
  }

  function _doSymbolSearch(keyword) {
    if (!keyword || keyword.length < 1) {
      var resultsEl = $('simSearchResults');
      if (resultsEl) resultsEl.innerHTML = '';
      return;
    }
    var url = F._API + '/sim/search?q=' + encodeURIComponent(keyword);
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        var resultsEl = $('simSearchResults');
        if (!resultsEl) return;
        var items = data.results || [];
        if (items.length === 0) {
          resultsEl.innerHTML = '<div class="sim-search-empty">未找到 "' + escHtml(keyword) + '"</div>';
          return;
        }
        var html = '';
        items.forEach(function (item) {
          var alreadyAdded = _customSymbols.some(function (s) { return s.code === item.code; });
          var icon = FUND_ICONS[item.code] || TYPE_ICONS[item.type] || '📄';
          var addCls = alreadyAdded ? ' sim-search-item-added' : '';
          var addBtn = alreadyAdded
            ? '<span class="sim-search-added-label">✓ 已添加</span>'
            : '<button class="sim-search-add-btn" onclick="TradingApp.addSymbol(\''
              + item.code + '\',\'' + escHtml(item.name || '').replace(/'/g, "\\'") + '\',\''
              + escHtml(item.type || '').replace(/'/g, "\\'") + '\')">+ 添加</button>';
          html += '<div class="sim-search-item' + addCls + '">'
            + '<span class="sim-search-icon">' + icon + '</span>'
            + '<span class="sim-search-name">' + escHtml(item.name || item.code) + '</span>'
            + '<span class="sim-search-code">' + escHtml(item.code) + '</span>'
            + '<span class="sim-search-type">' + escHtml(item.type || '') + '</span>'
            + addBtn
            + '</div>';
        });
        resultsEl.innerHTML = html;
      })
      .catch(function (err) {
        console.error('[Sim] Symbol search failed for q=' + keyword + ':', err);
      });
  }

  function onSymbolSearchInput(input) {
    var val = (input.value || '').trim();
    if (_searchTimer) clearTimeout(_searchTimer);
    if (!val) {
      var resultsEl = $('simSearchResults');
      if (resultsEl) resultsEl.innerHTML = '';
      return;
    }
    _searchTimer = setTimeout(function () { _doSymbolSearch(val); }, 300);
  }

  // ══════════════════════════════════════════
  //  Capital shortcuts
  // ══════════════════════════════════════════

  function setCapital(amount) {
    var el = $('simCapital');
    if (el) el.value = amount;
    document.querySelectorAll('.sim-cap-btn').forEach(function (b) {
      b.classList.toggle('active', parseInt(b.textContent) * 10000 === amount ||
        (b.textContent === '5万' && amount === 50000) ||
        (b.textContent === '10万' && amount === 100000) ||
        (b.textContent === '50万' && amount === 500000) ||
        (b.textContent === '100万' && amount === 1000000));
    });
  }

  // ══════════════════════════════════════════
  //  Phase Management
  // ══════════════════════════════════════════

  function _showPhase(phase) {
    // Don't write to sessionStorage here — caller uses _saveState() explicitly
    _simState = phase;
    Object.keys(PHASE_IDS).forEach(function (k) {
      var el = $(PHASE_IDS[k]);
      if (el) el.style.display = 'none';
    });
    var targetId = PHASE_IDS[phase];
    if (targetId) {
      var el = $(targetId);
      if (el) el.style.display = 'block';
    }
    var stepMap = { setup: 1, fetch: 2, run: 3, results: 4 };
    var currentStep = stepMap[phase] || 1;
    for (var i = 1; i <= 4; i++) {
      var stepEl = $('simStep' + i);
      if (!stepEl) continue;
      stepEl.classList.remove('sim-step-active', 'sim-step-done');
      if (i < currentStep) stepEl.classList.add('sim-step-done');
      else if (i === currentStep) stepEl.classList.add('sim-step-active');
    }
  }

  // ══════════════════════════════════════════
  //  Quick Period Selection
  // ══════════════════════════════════════════

  function setSimPeriod(days, btn) {
    var now = new Date();
    var end = _fmtDate(now);
    var start = _fmtDate(new Date(now.getTime() - days * 86400000));
    var startEl = $('simStartDate');
    var endEl = $('simEndDate');
    if (startEl) startEl.value = start;
    if (endEl) endEl.value = end;
    document.querySelectorAll('.sim-period-btn').forEach(function (b) {
      b.classList.remove('active');
    });
    if (btn) btn.classList.add('active');
  }

  // ══════════════════════════════════════════
  //  Fund name helpers (for display)
  // ══════════════════════════════════════════

  function _fundLabel(code) {
    var name = FUND_NAMES[code];
    var icon = FUND_ICONS[code] || '📊';
    return name ? icon + ' ' + name : code;
  }

  // ══════════════════════════════════════════
  //  Phase 2: Fetch Historical Data
  // ══════════════════════════════════════════

  function startFetchData() {
    var startDate = ($('simStartDate') || {}).value || '';
    var endDate = ($('simEndDate') || {}).value || '';
    if (!startDate || !endDate) { toast('请先选择模拟的起止日期', 'warn'); return; }

    var capital = parseFloat(($('simCapital') || {}).value || 100000);
    var symbols = _getSymbolList();
    if (symbols.length === 0) { toast('请至少添加一个投资标的 — 用下方搜索或快捷添加', 'warn'); return; }

    // ★ Reset state for new fetch
    _fetchLogHistory = [];
    _fetchPhasesDone = {};
    _fetchPhaseProgress = {};
    _fetchComplete = false;
    _simComplete = false;
    _simResultData = null;
    _simDecisionCount = 0;
    _simEquityPoints = [];
    _simTimelineEntries = [];
    _simTradeEntries = [];

    // ★ Save params to sessionStorage for refresh recovery
    _saveParams({
      startDate: startDate,
      endDate: endDate,
      strategyId: _strategyId,
      benchmarkIndex: _benchmarkIndex,
      symbols: symbols,
      capital: capital,
    });

    _saveState('fetch');
    _showPhase('fetch');

    // Init progress UI
    ['prices', 'indices', 'macro', 'intel'].forEach(function (p) {
      _setFetchProgress(p, 0, 0, '等待中...');
    });
    var overallEl = $('simFetchOverall');
    if (overallEl) { overallEl.style.width = '0%'; overallEl.textContent = '0%'; }
    var logEl = $('simFetchLog');
    if (logEl) logEl.innerHTML = '';

    _appendFetchLog('🚀 开始抓取历史数据...');
    _appendFetchLog('📅 时段: ' + startDate + ' → ' + endDate);
    _appendFetchLog('策略: AI 自主选择最优组合');
    _appendFetchLog('📊 正在连接服务器...');

    // ★ Elapsed timer
    _fetchStartTime = Date.now();
    if (_fetchElapsedTimer) clearInterval(_fetchElapsedTimer);
    _fetchElapsedTimer = setInterval(function () {
      if (_fetchComplete) { clearInterval(_fetchElapsedTimer); _fetchElapsedTimer = null; return; }
      var sec = Math.round((Date.now() - _fetchStartTime) / 1000);
      var elapsedEl = $('simFetchElapsed');
      if (elapsedEl) elapsedEl.textContent = '已用时 ' + sec + ' 秒';
    }, 1000);

    // ★ Cancel any existing poll timer
    _stopFetchPoll();

    var url = F._API + '/sim/fetch-data';
    var body = JSON.stringify({
      symbols: symbols,
      start_date: startDate,
      end_date: endDate,
      skip_intel: ($('simSkipIntel') || {}).checked || false,
      symbol_names: _getSymbolNames(),
    });

    fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: body,
    }).then(function (resp) { return resp.json(); })
    .then(function (data) {
      if (data.error) {
        toast('启动失败: ' + data.error, 'error');
        _appendFetchLog('❌ ' + data.error);
        _stopFetchElapsed();
        return;
      }
      var taskId = data.task_id;
      // ★ Save task_id to sessionStorage for refresh recovery
      sessionStorage.setItem(SK_FETCH_TASK, taskId);
      _appendFetchLog('✅ 服务器已连接 (任务 ' + taskId + ')');

      _startFetchPoll(taskId, 0);
    })
    .catch(function (err) {
      console.error('[Sim] startFetchData request failed:', err);
      _stopFetchElapsed();
      toast('数据抓取失败: ' + err.message, 'error');
      _appendFetchLog('❌ 请求失败: ' + err.message);
    });
  }

  /**
   * ★ Start polling for fetch progress events.
   */
  function _startFetchPoll(taskId, cursor) {
    var pollUrl = F._API + '/sim/fetch-progress/' + taskId;
    var _cursor = cursor;

    _activeFetchPoll = setInterval(function () {
      fetch(pollUrl + '?cursor=' + _cursor)
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp.error === 'Task not found') {
            _stopFetchPoll();
            _stopFetchElapsed();
            // Task expired on server — show message
            if (!_fetchComplete) {
              _appendFetchLog('⚠️ 任务已过期，请重新开始');
            }
            return;
          }

          var events = resp.events || [];
          for (var i = 0; i < events.length; i++) {
            _handleFetchEvent(events[i]);
          }
          _cursor = resp.cursor || _cursor;

          if (resp.done) {
            _stopFetchPoll();
            _fetchComplete = true;
            _stopFetchElapsed();
            _updateOverallProgress();

            if (resp.error) {
              _appendFetchLog('❌ 出错了: ' + resp.error);
              toast('数据抓取出错', 'error');
            } else {
              if (resp.result) {
                _showFetchSummary(resp.result);
              } else {
                _appendFetchLog('✅ 数据抓取完成!');
              }
              var btnEl = $('simProceedToRun');
              if (btnEl) btnEl.style.display = 'inline-flex';
            }
          }
        })
        .catch(function (err) {
          console.error('[Sim] Fetch poll error (task=' + taskId + '):', err);
        });
    }, 1500);
  }

  /**
   * ★ Resume fetch polling after browser refresh.
   *   Replays ALL events from cursor=0 to rebuild log + progress bars.
   */
  function _resumeFetchPoll(taskId) {
    // Reset UI to "loading" state
    _fetchLogHistory = [];
    _fetchPhasesDone = {};
    _fetchPhaseProgress = {};
    _fetchComplete = false;

    // Init progress UI
    ['prices', 'indices', 'macro', 'intel'].forEach(function (p) {
      _setFetchProgress(p, 0, 0, '恢复中...');
    });
    var overallEl = $('simFetchOverall');
    if (overallEl) { overallEl.style.width = '0%'; overallEl.textContent = '0%'; }
    var logEl = $('simFetchLog');
    if (logEl) logEl.innerHTML = '';

    _appendFetchLog('🔄 页面已刷新，正在恢复数据抓取进度...');

    // ★ Start elapsed timer (approximate — don't know original start time)
    _fetchStartTime = Date.now();
    if (_fetchElapsedTimer) clearInterval(_fetchElapsedTimer);
    _fetchElapsedTimer = setInterval(function () {
      if (_fetchComplete) { clearInterval(_fetchElapsedTimer); _fetchElapsedTimer = null; return; }
      var elapsedEl = $('simFetchElapsed');
      if (elapsedEl) elapsedEl.textContent = '恢复中...';
    }, 1000);

    // Start polling from cursor=0 to replay all events
    _startFetchPoll(taskId, 0);
  }

  function _stopFetchPoll() {
    if (_activeFetchPoll) {
      clearInterval(_activeFetchPoll);
      _activeFetchPoll = null;
    }
  }

  function _stopFetchElapsed() {
    if (_fetchElapsedTimer) {
      clearInterval(_fetchElapsedTimer);
      _fetchElapsedTimer = null;
    }
  }

  /**
   * ★ Centralized fetch event handler — updates both UI and persistent state.
   */
  function _handleFetchEvent(evt) {
    if (evt.phase) {
      _setFetchProgress(evt.phase, evt.done || 0, evt.total || 0, evt.message || '');
      _fetchPhaseProgress[evt.phase] = { done: evt.done || 0, total: evt.total || 0, msg: evt.message || '' };
      if (evt.message) _appendFetchLog(evt.message);
      if (evt.done > 0 && evt.total > 0 && evt.done >= evt.total) {
        _fetchPhasesDone[evt.phase] = true;
      }
      _updateOverallProgress();
    }
    if (evt.error) {
      _appendFetchLog('❌ 出错了: ' + evt.error);
      toast('数据抓取出错', 'error');
    }
    if (evt.phases || evt.duration_seconds !== undefined) {
      _showFetchSummary(evt);
    }
  }

  function _updateOverallProgress() {
    var overallEl = $('simFetchOverall');
    if (!overallEl) return;
    if (_fetchComplete) {
      overallEl.style.width = '100%';
      overallEl.textContent = '100%';
      return;
    }
    var phases = ['deps', 'prices', 'indices', 'macro', 'intel'];
    var weights = [5, 25, 20, 15, 35];
    var total = 0;
    for (var i = 0; i < phases.length; i++) {
      var ph = phases[i];
      var w = weights[i];
      if (_fetchPhasesDone[ph]) {
        total += w;
      } else {
        var p = _fetchPhaseProgress[ph];
        if (p && p.total > 0) {
          total += Math.round((p.done / p.total) * w);
        }
      }
    }
    var pct = Math.min(99, total);
    overallEl.style.width = pct + '%';
    overallEl.textContent = pct + '%';
  }

  function _setFetchProgress(phase, done, total, msg) {
    var barEl = $('simProg_' + phase);
    var textEl = $('simProgText_' + phase);
    if (barEl && total > 0) {
      var pct = Math.min(100, Math.round(done / total * 100));
      barEl.style.width = pct + '%';
    }
    if (textEl) textEl.textContent = msg || (done + '/' + total);
  }

  function _appendFetchLog(msg) {
    var time = new Date().toTimeString().slice(0, 8);
    _fetchLogHistory.push({ time: time, msg: msg });

    var el = $('simFetchLog');
    if (!el) return;
    var line = document.createElement('div');
    line.className = 'sim-log-line';
    line.innerHTML = '<span class="sim-log-time">' + time + '</span> ' + escHtml(msg);
    el.appendChild(line);
    el.scrollTop = el.scrollHeight;
  }

  /**
   * ★ Replay accumulated log history on SPA page-switch (not full refresh).
   */
  function _replayFetchLogs() {
    var logEl = $('simFetchLog');
    if (!logEl) return;
    logEl.innerHTML = '';
    _fetchLogHistory.forEach(function (entry) {
      var line = document.createElement('div');
      line.className = 'sim-log-line';
      line.innerHTML = '<span class="sim-log-time">' + entry.time + '</span> ' + escHtml(entry.msg);
      logEl.appendChild(line);
    });
    logEl.scrollTop = logEl.scrollHeight;

    Object.keys(_fetchPhaseProgress).forEach(function (phase) {
      var p = _fetchPhaseProgress[phase];
      _setFetchProgress(phase, p.done, p.total, p.msg);
    });

    _updateOverallProgress();
  }

  function _showFetchSummary(data) {
    if (!data) return;
    _fetchComplete = true;
    _updateOverallProgress();
    var btnEl = $('simProceedToRun');
    if (btnEl) btnEl.style.display = 'inline-flex';

    _appendFetchLog('══════════════════════════');

    var phases = data.phases || data;
    var priceData = phases.prices || {};
    Object.keys(priceData).forEach(function (sym) {
      var info = priceData[sym];
      var count = info.count || 0;
      var status = info.status || '';
      var statusIcon = status === 'cached' ? '💾' : status === 'fetched' ? '📈' : '⚠️';
      _appendFetchLog(statusIcon + ' ' + _fundLabel(sym) + ': ' + count + ' 天数据' +
        (info.first_date ? ' (' + info.first_date + '~' + info.last_date + ')' : ''));
    });

    var intelData = phases.intel || {};
    if (intelData.total_fetched !== undefined) {
      _appendFetchLog('📰 新闻情报: ' + (intelData.total_fetched || 0) + ' 条');
    }

    if (data.duration_seconds) {
      _appendFetchLog('⏱️ 总耗时: ' + data.duration_seconds + ' 秒');
    }

    _appendFetchLog('✅ 全部就绪，可以开始模拟了!');
  }

  function proceedToRun() {
    _saveState('run');
    _showPhase('run');
  }

  function skipFetchAndRun() {
    _saveState('run');
    _showPhase('run');
  }

  // ══════════════════════════════════════════
  //  Phase 3: Run LLM Simulation (POLLING mode)
  // ══════════════════════════════════════════

  function startSimulation() {
    var params = _loadParams();
    var symbols = params.symbols || _getSymbolList();
    var startDate = params.startDate || ($('simStartDate') || {}).value || '';
    var endDate = params.endDate || ($('simEndDate') || {}).value || '';
    var capital = parseFloat(params.capital || ($('simCapital') || {}).value || 100000);
    var stepDays = parseInt(($('simStepDays') || {}).value || 5, 10);

    if (!startDate || !endDate) { toast('请先设置日期', 'warn'); return; }

    _simInitialCapital = capital;
    _simDecisionCount = 0;
    _simEquityPoints = [{ date: startDate, value: capital }];
    _simTimelineEntries = [];
    _simTradeEntries = [];
    _simComplete = false;
    _simResultData = null;

    _setSimRunning(true);

    var timelineEl = $('simTimeline');
    if (timelineEl) timelineEl.innerHTML = '';
    var reasoningEl = $('simReasoning');
    if (reasoningEl) { reasoningEl.innerHTML = ''; reasoningEl.style.display = 'none'; }
    var analysisEl = $('simAnalysisPanel');
    if (analysisEl) { analysisEl.innerHTML = ''; analysisEl.style.display = 'none'; }
    _resetEquityChart();
    _resetSimPipeline();

    _stopSimPoll();

    var url = F._API + '/sim/run';
    var body = JSON.stringify({
      symbols: symbols,
      start_date: startDate,
      end_date: endDate,
      initial_capital: capital,
      step_days: stepDays,
      strategy: _strategyId,
      benchmark_index: _benchmarkIndex,
      stop_loss_pct: parseFloat(($('simStopLoss') || {}).value || 5),
      take_profit_pct: parseFloat(($('simTakeProfit') || {}).value || 15),
      max_position_pct: parseFloat(($('simMaxPos') || {}).value || 30),
      min_confidence: parseFloat(($('simMinConf') || {}).value || 50),
    });

    fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body })
    .then(function (resp) { return resp.json(); })
    .then(function (data) {
      if (data.error) {
        toast('模拟启动失败: ' + data.error, 'error');
        _resetRunBtn();
        return;
      }
      var taskId = data.task_id;
      // ★ Save task_id for refresh recovery
      sessionStorage.setItem(SK_SIM_TASK, taskId);
      _saveState('run');

      _startSimPoll(taskId, 0);
    })
    .catch(function (err) {
      console.error('[Sim] startSimulation request failed:', err);
      toast('模拟启动失败: ' + err.message, 'error');
      _resetRunBtn();
    });
  }

  /**
   * ★ Start polling for simulation progress events.
   */
  function _startSimPoll(taskId, cursor) {
    var pollUrl = F._API + '/sim/run-progress/' + taskId;
    var _cursor = cursor;

    _activeSimPoll = setInterval(function () {
      fetch(pollUrl + '?cursor=' + _cursor)
        .then(function (r) { return r.json(); })
        .then(function (resp) {
          if (resp.error === 'Task not found') {
            _stopSimPoll();
            if (!_simComplete) {
              toast('模拟任务已过期', 'warn');
              _resetRunBtn();
            }
            return;
          }

          var events = resp.events || [];
          for (var i = 0; i < events.length; i++) {
            _handleSimEvent(events[i]);
          }
          _cursor = resp.cursor || _cursor;

          if (resp.done) {
            _stopSimPoll();
            if (!_simComplete) {
              // Final result from server
              if (resp.error) {
                toast('模拟出错: ' + resp.error, 'error');
                _resetRunBtn();
              } else if (resp.result) {
                _simResultData = resp.result;
                _simComplete = true;
                _activeSessionId = resp.result.session_id;
                _setSimPipelineStage(5);
                setTimeout(function () { _showResults(resp.result); }, 500);
              }
            }
          }
        })
        .catch(function (err) {
          console.error('[Sim] Run poll error (task=' + taskId + '):', err);
        });
    }, 1500);
  }

  /**
   * ★ Resume sim polling after browser refresh.
   *   Replays ALL events from cursor=0 to rebuild timeline + equity chart.
   */
  function _resumeSimPoll(taskId) {
    // Reset UI state for replay
    _simDecisionCount = 0;
    _simEquityPoints = [];
    _simTimelineEntries = [];
    _simTradeEntries = [];
    _simComplete = false;
    _simResultData = null;

    var params = _loadParams();
    var startDate = params.startDate || '';
    var capital = parseFloat(params.capital || 100000);
    _simInitialCapital = capital;
    if (startDate) _simEquityPoints = [{ date: startDate, value: capital }];

    _setSimRunning(true);

    var timelineEl = $('simTimeline');
    if (timelineEl) timelineEl.innerHTML = '';
    _resetEquityChart();
    _resetSimPipeline();

    var progEl = $('simRunProgress');
    if (progEl) progEl.textContent = '🔄 页面已刷新，正在恢复模拟进度...';

    _startSimPoll(taskId, 0);
  }

  function _stopSimPoll() {
    if (_activeSimPoll) {
      clearInterval(_activeSimPoll);
      _activeSimPoll = null;
    }
  }

  function _setSimRunning(running) {
    var btn = $('simRunBtn');
    var runIndicator = $('simRunningIndicator');
    if (running) {
      if (btn) { btn.disabled = true; btn.innerHTML = '<span class="sim-spinner"></span> AI 正在模拟操盘中...'; }
      if (runIndicator) runIndicator.style.display = 'flex';
    } else {
      if (btn) { btn.disabled = false; btn.innerHTML = '🚀 开始模拟'; }
      if (runIndicator) runIndicator.style.display = 'none';
    }
  }

  /**
   * ★ Centralized simulation event handler.
   */
  function _handleSimEvent(evt) {
    var type = evt._type || '';
    var reasoningEl = $('simReasoning');

    switch (type) {
      case 'sim_start':
        _activeSessionId = evt.session_id;
        var progEl = $('simRunProgress');
        if (progEl) progEl.textContent = '共 ' + (evt.total_steps || '?') + ' 个决策点，AI 开始思考...';
        break;

      case 'sim_step':
        _setSimPipelineStage(1);
        var progEl2 = $('simRunProgress');
        if (progEl2) progEl2.textContent = '第 ' + evt.step + '/' + (evt.total || '?') + ' 步 · ' + (evt.sim_date || '') + ' 准备数据...';
        break;

      case 'sim_analyzing':
        _renderAnalysisPanel(evt);
        _setSimPipelineStage(3);
        var progElA = $('simRunProgress');
        if (progElA) progElA.textContent = '第 ' + evt.step + '/' + (evt.total || '?') + ' 步 · ' + (evt.sim_date || '') + ' AI 正在分析决策...';
        break;

      case 'sim_step_done':
        _simDecisionCount++;
        _addTimelineEntry(evt, _simDecisionCount);
        // ★ Persist for replay
        _simTimelineEntries.push({ evt: evt, step: _simDecisionCount });
        _clearAnalysisPanel();
        _setSimPipelineStage(5);
        if (evt.portfolio_value) {
          _simEquityPoints.push({ date: evt.sim_date, value: evt.portfolio_value });
          _renderMiniEquity(_simEquityPoints, _simInitialCapital);
        }
        var progEl3 = $('simRunProgress');
        if (progEl3) progEl3.textContent = '第 ' + _simDecisionCount + ' 步完成 · ' + (evt.sim_date || '') + ' · ¥' + fmtNum(evt.portfolio_value || 0);
        break;

      case 'sim_trade':
        _addTradeToTimeline(evt);
        _simTradeEntries.push(evt);
        _setSimPipelineStage(4);
        break;

      case 'sim_error':
        toast('模拟出错: ' + (evt.error || '未知错误'), 'error');
        _resetRunBtn();
        break;

      case 'sim_complete':
        _activeSessionId = evt.session_id;
        _simResultData = evt;
        _simComplete = true;
        _setSimPipelineStage(5);
        setTimeout(function () { _showResults(evt); }, 500);
        break;

      default:
        if (evt.metrics && evt.session_id) {
          if (_simResultData && _simComplete) {
            if (!_simResultData.trade_count && evt.trade_count) _simResultData.trade_count = evt.trade_count;
            if (!_simResultData.total_fees && evt.total_fees) _simResultData.total_fees = evt.total_fees;
            _setKPI('simResTrades', String(_simResultData.trade_count || 0), '');
            _setKPI('simResFees', '¥' + fmtNum(_simResultData.total_fees || 0), '');
          } else if (!_simComplete) {
            _activeSessionId = evt.session_id;
            _simResultData = evt;
            _simComplete = true;
            _setSimPipelineStage(5);
            setTimeout(function () { _showResults(evt); }, 500);
          }
        }
        if (evt.error) {
          toast('模拟出错: ' + evt.error, 'error');
          _resetRunBtn();
        }
        break;
    }

    if (evt.reasoning && reasoningEl) {
      reasoningEl.style.display = 'block';
      reasoningEl.innerHTML = '<div class="sim-reasoning-label">AI 正在思考:</div>' + renderMarkdown(evt.reasoning);
    }
  }

  /**
   * ★ Restore simulation run UI on SPA page-switch (not full refresh).
   */
  function _restoreSimRunUI() {
    if (_simComplete && _simResultData) {
      _showResults(_simResultData);
      return;
    }

    if (_activeSimPoll) {
      _setSimRunning(true);
      var progEl = $('simRunProgress');
      if (progEl) progEl.textContent = '第 ' + _simDecisionCount + ' 步 · 运行中...';
    }

    // Re-render timeline from persisted entries
    var timelineEl = $('simTimeline');
    if (timelineEl) {
      timelineEl.innerHTML = '';
      _simTimelineEntries.forEach(function (entry) {
        _addTimelineEntry(entry.evt, entry.step);
      });
    }

    if (_simEquityPoints.length > 1) {
      _renderMiniEquity(_simEquityPoints, _simInitialCapital);
    }
  }

  // ── Analysis Panel ──

  function _renderAnalysisPanel(evt) {
    var el = $('simAnalysisPanel');
    if (!el) return;
    el.style.display = 'block';

    var holdingsHtml = '';
    if (evt.holdings && evt.holdings.length > 0) {
      holdingsHtml = evt.holdings.map(function (h) {
        var sign = h.pnl_pct >= 0 ? '+' : '';
        return '<span class="sim-ap-tag">' + _fundLabel(h.symbol) +
          ' <small class="' + pnlClass(h.pnl_pct) + '">' + sign + fmtNum(h.pnl_pct, 1) + '%</small></span>';
      }).join(' ');
    } else {
      holdingsHtml = '<span class="sim-ap-empty-tag">空仓（未持有任何标的）</span>';
    }

    var signalsHtml = '';
    if (evt.signals && evt.signals.length > 0) {
      signalsHtml = '<div class="sim-ap-signals">' + evt.signals.map(function (s) {
        if (s.data === 'insufficient' || s.data === 'error') {
          return '<div class="sim-ap-sig-row"><span class="sim-ap-sig-name">' + _fundLabel(s.symbol) + '</span><span class="sim-ap-no-data">数据不足</span></div>';
        }
        var trendIcon = s.trend === 'bullish' ? '📈' : s.trend === 'bearish' ? '📉' : '↔️';
        var trendText = s.trend === 'bullish' ? '多头排列' : s.trend === 'bearish' ? '空头排列' : '震荡整理';
        var retSign = s.ret_5d >= 0 ? '+' : '';
        var rsiClass = s.rsi > 70 ? 'sim-ap-rsi-high' : s.rsi < 30 ? 'sim-ap-rsi-low' : '';
        return '<div class="sim-ap-sig-row">' +
          '<span class="sim-ap-sig-name">' + _fundLabel(s.symbol) + '</span>' +
          '<span class="sim-ap-sig-trend">' + trendIcon + ' ' + trendText + '</span>' +
          '<span class="sim-ap-sig-rsi ' + rsiClass + '">RSI ' + s.rsi + '</span>' +
          '<span class="' + pnlClass(s.ret_5d) + '">5日 ' + retSign + fmtNum(s.ret_5d, 1) + '%</span>' +
        '</div>';
      }).join('') + '</div>';
    }

    var retPct = evt.return_pct || 0;
    var retSign = retPct >= 0 ? '+' : '';

    el.innerHTML =
      '<div class="sim-ap-header">' +
        '<span class="sim-ap-pulse"></span>' +
        '<span class="sim-ap-title">🔍 第 ' + (evt.step || '') + '/' + (evt.total || '') + ' 步 · AI 正在分析 <strong>' + escHtml(evt.sim_date || '') + '</strong> 的市场数据</span>' +
      '</div>' +
      '<div class="sim-ap-body">' +
        '<div class="sim-ap-row">' +
          '<div class="sim-ap-card">' +
            '<div class="sim-ap-label">💰 账户状态</div>' +
            '<div class="sim-ap-vals">' +
              '<span>现金 ¥' + fmtNum(evt.cash || 0) + '</span>' +
              '<span>总值 ¥' + fmtNum(evt.portfolio_value || 0) + '</span>' +
              '<span class="' + pnlClass(retPct) + '">' + retSign + fmtNum(retPct, 2) + '%</span>' +
            '</div>' +
          '</div>' +
          '<div class="sim-ap-card">' +
            '<div class="sim-ap-label">📦 当前持仓</div>' +
            '<div class="sim-ap-vals">' + holdingsHtml + '</div>' +
          '</div>' +
        '</div>' +
        (signalsHtml ? '<div class="sim-ap-card sim-ap-wide"><div class="sim-ap-label">📊 各标的技术信号</div>' + signalsHtml + '</div>' : '') +
        '<div class="sim-ap-footer">' +
          '<span>📰 ' + (evt.intel_count || 0) + ' 条新闻情报</span>' +
          (evt.has_market_ctx ? '<span>🏛️ 宏观数据已加载</span>' : '') +
          '<span class="sim-ap-thinking">AI 正在综合分析做出决策...</span>' +
        '</div>' +
      '</div>';
  }

  function _clearAnalysisPanel() {
    var el = $('simAnalysisPanel');
    if (el) el.style.display = 'none';
  }

  function _resetRunBtn() {
    _setSimRunning(false);
  }

  // ── Timeline entries ──

  function _addTimelineEntry(evt, step) {
    var el = $('simTimeline');
    if (!el) return;

    var actions = evt.actions || [];
    var actionHtml = '';
    if (actions.length === 0) {
      actionHtml = '<span class="sim-tl-hold">📋 观望不动</span>';
    } else {
      actionHtml = actions.map(function (a) {
        var verb = a.action === 'buy' ? '🟢 买入' : a.action === 'sell' ? '🔴 卖出' : '⏸ 持有';
        var cls = a.action === 'buy' ? 'sim-tl-buy' : a.action === 'sell' ? 'sim-tl-sell' : 'sim-tl-hold';
        return '<span class="' + cls + '">' + verb + ' ' + escHtml(_fundLabel(a.symbol || '')) +
          (a.amount ? ' ¥' + fmtNum(a.amount) : '') + '</span>';
      }).join('  ');
    }

    var pv = evt.portfolio_value || 0;
    var retPct = evt.return_pct || 0;
    var retSign = retPct >= 0 ? '+' : '';

    // Strategies used tags
    var stratTags = '';
    var stratNames = evt.strategies_used || [];
    if (stratNames.length > 0) {
      stratTags = '<div class="sim-tl-strats">' +
        stratNames.map(function (n) { return '<span class="sim-tl-strat-tag">' + escHtml(n) + '</span>'; }).join('') +
      '</div>';
    }

    var entry = document.createElement('div');
    entry.className = 'sim-tl-entry';
    entry.innerHTML =
      '<div class="sim-tl-dot"></div>' +
      '<div class="sim-tl-content">' +
        '<div class="sim-tl-header">' +
          '<span class="sim-tl-date">' + escHtml(evt.sim_date || '第' + step + '步') + '</span>' +
          '<span class="sim-tl-step">第' + step + '次</span>' +
          '<span class="sim-tl-equity ' + pnlClass(retPct) + '">¥' + fmtNum(pv) + ' <small>(' + retSign + fmtNum(retPct, 2) + '%)</small></span>' +
        '</div>' +
        '<div class="sim-tl-actions">' + actionHtml + '</div>' +
        stratTags +
        (evt.summary ? '<div class="sim-tl-summary">' + escHtml(evt.summary).substring(0, 200) + '</div>' : '') +
        (evt.reasoning ? '<details class="sim-tl-reasoning"><summary>💡 看看 AI 的分析过程</summary><div class="sim-tl-reasoning-body">' + renderMarkdown(evt.reasoning) + '</div></details>' : '') +
      '</div>';
    el.appendChild(entry);
    el.scrollTop = el.scrollHeight;
  }

  function _addTradeToTimeline(trade) {
    var el = $('simTimeline');
    if (!el) return;
    var entry = document.createElement('div');
    entry.className = 'sim-tl-trade';
    entry.innerHTML =
      '<span>' + (trade.action === 'buy' ? '🟢 买入' : '🔴 卖出') + ' ' + escHtml(_fundLabel(trade.symbol || '')) + '</span>' +
      (trade.amount ? '<span>¥' + fmtNum(trade.amount) + '</span>' : '') +
      (trade.price ? '<span>@ ' + fmtNum(trade.price, 4) + '</span>' : '');
    el.appendChild(entry);
  }

  // ── Mini Equity Chart (canvas) ──

  function _resetEquityChart() {
    var container = $('simEquityMini');
    if (container) container.innerHTML = '<div class="sim-equity-placeholder">📈 模拟开始后这里会实时显示资产变化曲线</div>';
  }

  function _renderMiniEquity(points, initialCapital) {
    var container = $('simEquityMini');
    if (!container || points.length < 2) return;

    var canvas = container.querySelector('canvas');
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.width = (container.offsetWidth || 400) * 2;
      canvas.height = 200;
      canvas.style.width = '100%';
      canvas.style.height = '100px';
      container.innerHTML = '';
      container.appendChild(canvas);
    }

    var ctx = canvas.getContext('2d');
    var w = canvas.width, h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    var values = points.map(function (p) { return p.value; });
    var minV = Math.min.apply(null, values);
    var maxV = Math.max.apply(null, values);
    var range = maxV - minV || 1;
    var pad = 10;

    ctx.fillStyle = 'rgba(6,8,13,0.6)';
    ctx.fillRect(0, 0, w, h);

    var baseY = h - pad - ((initialCapital - minV) / range) * (h - 2 * pad);
    ctx.strokeStyle = 'rgba(255,255,255,0.12)';
    ctx.setLineDash([6, 6]);
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, baseY); ctx.lineTo(w, baseY); ctx.stroke();
    ctx.setLineDash([]);

    var lastVal = values[values.length - 1];
    var color = lastVal >= initialCapital ? '#00E59B' : '#FF4D6A';
    ctx.strokeStyle = color;
    ctx.lineWidth = 3;
    ctx.lineJoin = 'round';
    ctx.beginPath();
    for (var i = 0; i < points.length; i++) {
      var x = pad + (i / (points.length - 1)) * (w - 2 * pad);
      var y = h - pad - ((values[i] - minV) / range) * (h - 2 * pad);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    }
    ctx.stroke();

    ctx.lineTo(pad + (w - 2 * pad), h);
    ctx.lineTo(pad, h);
    ctx.closePath();
    var grad = ctx.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, color === '#00E59B' ? 'rgba(0,229,155,0.15)' : 'rgba(255,77,106,0.15)');
    grad.addColorStop(1, 'rgba(0,0,0,0)');
    ctx.fillStyle = grad;
    ctx.fill();

    ctx.font = 'bold 22px Inter, sans-serif';
    ctx.fillStyle = color;
    ctx.textAlign = 'right';
    var retPct = ((lastVal - initialCapital) / initialCapital * 100);
    var retText = (retPct >= 0 ? '+' : '') + retPct.toFixed(1) + '%';
    ctx.fillText('¥' + fmtNum(lastVal) + ' (' + retText + ')', w - pad, 28);
  }

  // ── Simulation Pipeline ──

  function _resetSimPipeline() {
    for (var i = 1; i <= 5; i++) {
      var el = $('simPipe' + i);
      if (el) el.classList.remove('sim-pipe-active', 'sim-pipe-done');
    }
  }

  function _setSimPipelineStage(n) {
    for (var i = 1; i <= 5; i++) {
      var el = $('simPipe' + i);
      if (!el) continue;
      el.classList.remove('sim-pipe-active', 'sim-pipe-done');
      if (i < n) el.classList.add('sim-pipe-done');
      else if (i === n) el.classList.add('sim-pipe-active');
    }
  }

  // ══════════════════════════════════════════
  //  Phase 4: Show Results
  // ══════════════════════════════════════════

  function _showResults(data) {
    _saveState('results');
    _showPhase('results');
    _simResultData = data;
    var metrics = data.metrics || {};
    var totalReturn = metrics.total_return_pct || 0;
    var maxDD = metrics.max_drawdown_pct || 0;
    var sharpe = metrics.sharpe_ratio || 0;
    var winRate = metrics.win_rate || 0;

    var heroEl = $('simResHero');
    if (heroEl) {
      var sign = totalReturn >= 0 ? '+' : '';
      heroEl.className = 'sim-res-hero-value ' + pnlClass(totalReturn);
      heroEl.textContent = sign + fmtNum(totalReturn, 2) + '%';
    }
    var heroLabel = $('simResHeroLabel');
    if (heroLabel) {
      heroLabel.textContent = totalReturn >= 0
        ? '🎉 恭喜！AI 在真实历史行情中赚钱了！'
        : '📉 这段时间市场不好，AI 也亏了。换个时间段再试试？';
    }

    _setKPI('simResReturn', (totalReturn >= 0 ? '+' : '') + fmtNum(totalReturn, 2) + '%', pnlClass(totalReturn));
    _setKPI('simResDrawdown', fmtNum(maxDD, 2) + '%', maxDD > 10 ? 'down' : '');
    _setKPI('simResSharpe', fmtNum(sharpe, 2), sharpe > 1 ? 'up' : sharpe < 0 ? 'down' : '');
    _setKPI('simResWinRate', fmtNum(winRate, 1) + '%', winRate > 50 ? 'up' : 'down');
    _setKPI('simResTrades', String(data.trade_count || 0), '');
    _setKPI('simResFees', '¥' + fmtNum(data.total_fees || 0), '');

    var sharpeHint = $('simResSharpeHint');
    if (sharpeHint) {
      if (sharpe > 1.5) sharpeHint.textContent = '非常优秀！冒的风险很值得';
      else if (sharpe > 1) sharpeHint.textContent = '不错，赚的比冒的风险多';
      else if (sharpe > 0) sharpeHint.textContent = '一般般，赚的刚够覆盖风险';
      else sharpeHint.textContent = '不太行，冒了风险却没赚到';
    }

    var bench = data.benchmark || {};
    var benchName = bench.name || BENCHMARK_NAMES[_benchmarkIndex] || '沪深300';
    var benchEl = $('simResBenchmark');
    if (benchEl) {
      var benchRet = bench.return_pct || 0;
      var alpha = totalReturn - benchRet;
      var alphaSign = alpha >= 0 ? '+' : '';
      benchEl.innerHTML =
        '<div class="sim-bench-title">📊 和「什么都不做直接买' + benchName + '」比一比</div>' +
        '<div class="sim-bench-row">' +
          '<span class="sim-bench-label"><svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="vertical-align:-2px"><path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3 4 4.5 4.5 0 0 1-3-4"/><path d="M12 18v4"/><path d="M8 18l-2 2"/><path d="M16 18l2 2"/></svg> AI 的成绩</span>' +
          '<span class="sim-bench-val ' + pnlClass(totalReturn) + '">' + (totalReturn >= 0 ? '+' : '') + fmtNum(totalReturn, 2) + '%</span>' +
        '</div>' +
        '<div class="sim-bench-row">' +
          '<span class="sim-bench-label">📉 直接买' + benchName + '</span>' +
          '<span class="sim-bench-val ' + pnlClass(benchRet) + '">' + (benchRet >= 0 ? '+' : '') + fmtNum(benchRet, 2) + '%</span>' +
        '</div>' +
        '<div class="sim-bench-row sim-bench-alpha">' +
          '<span class="sim-bench-label">✨ AI 比' + benchName + '多赚了</span>' +
          '<span class="sim-bench-val ' + pnlClass(alpha) + ' sim-alpha-value">' + alphaSign + fmtNum(alpha, 2) + '%</span>' +
        '</div>' +
        '<div class="sim-bench-hint">' +
          (alpha > 0 ? '👍 <strong>AI 比直接买' + benchName + '多赚了 ' + alphaSign + fmtNum(alpha, 2) + '%</strong>，说明 AI 的选择是有价值的！值得信赖 ✨'
                     : '👎 这次 AI 还不如直接买' + benchName + '。不过市场总有起伏，试试其他时间段看看？') +
        '</div>';
    }

    if (data.session_id) _loadResultJournal(data.session_id);

    var ctaEl = $('simResCTA');
    if (ctaEl) {
      if (totalReturn > 0) {
        ctaEl.innerHTML =
          '<div class="sim-cta-box sim-cta-positive">' +
            '<div class="sim-cta-icon">🎉</div>' +
            '<div class="sim-cta-text">' +
              '<strong>AI 在真实历史行情中赚了 ' + (totalReturn >= 0 ? '+' : '') + fmtNum(totalReturn, 2) + '%！</strong><br>' +
              '<span>觉得 AI 靠谱？让它帮你管理真实投资吧</span>' +
            '</div>' +
            '<button class="btn ov-btn-cta" onclick="TradingApp.navigate(\'brain\')">让 AI 帮我选</button>' +
          '</div>';
      } else {
        ctaEl.innerHTML =
          '<div class="sim-cta-box sim-cta-neutral">' +
            '<div class="sim-cta-icon">💡</div>' +
            '<div class="sim-cta-text">' +
              '<strong>市场总有涨跌，AI 也不是每次都赚</strong><br>' +
              '<span>试试其他时间段，或者换个风险偏好再模拟一次</span>' +
            '</div>' +
            '<button class="btn ov-btn-secondary" onclick="TradingApp.navigate(\'simulator\')">🔄 再试一次</button>' +
          '</div>';
      }
    }

    _resetRunBtn();
    toast('✅ 模拟完成!', 'success');
  }

  function _setKPI(id, value, cls) {
    var el = $(id);
    if (!el) return;
    el.textContent = value;
    el.className = 'sim-res-value ' + (cls || '');
  }

  function _loadResultJournal(sessionId) {
    api('/sim/journal/' + sessionId + '?limit=200&type=step_summary').then(function (data) {
      var entries = data.journal || [];
      var el = $('simResJournal');
      if (!el) return;

      if (entries.length === 0) {
        el.innerHTML = '<div class="sim-empty">暂无操作记录</div>';
        return;
      }

      el.innerHTML = entries.map(function (e, idx) {
        var sig = e.signals || {};
        var actions = sig.actions || [];
        var portfolioValue = sig.portfolio_value || e.amount || 0;
        var retPct = sig.return_pct || 0;
        var summary = sig.summary || '';

        var actionsHtml = actions.length > 0
          ? actions.map(function (a) {
              var cls = a.action === 'buy' ? 'sim-j-buy' : a.action === 'sell' ? 'sim-j-sell' : 'sim-j-hold';
              var verb = a.action === 'buy' ? '🟢 买入' : a.action === 'sell' ? '🔴 卖出' : '⏸ 持有';
              return '<span class="sim-j-action ' + cls + '">' + verb +
                ' ' + escHtml(_fundLabel(a.symbol || '')) +
                (a.amount ? ' ¥' + fmtNum(a.amount) : '') + '</span>';
            }).join(' ')
          : '<span class="sim-j-hold">📋 观望不动</span>';

        var retSign = retPct >= 0 ? '+' : '';

        return '<div class="sim-j-entry">' +
          '<div class="sim-j-header">' +
            '<span class="sim-j-num">#' + (idx + 1) + '</span>' +
            '<span class="sim-j-date">' + escHtml(e.sim_date || '') + '</span>' +
            '<span class="sim-j-equity ' + pnlClass(retPct) + '">¥' + fmtNum(portfolioValue) + '</span>' +
            '<span class="sim-j-ret ' + pnlClass(retPct) + '">' + retSign + fmtNum(retPct, 2) + '%</span>' +
          '</div>' +
          '<div class="sim-j-actions">' + actionsHtml + '</div>' +
          (summary ? '<div class="sim-j-summary">' + escHtml(summary).substring(0, 200) + '</div>' : '') +
          (e.reasoning ? '<details class="sim-j-reasoning"><summary>💡 看看 AI 当时怎么想的</summary><div class="sim-j-reasoning-body">' + renderMarkdown(e.reasoning) + '</div></details>' : '') +
        '</div>';
      }).join('');
    }).catch(function (e) {
      console.error('[Sim] Journal load failed for session:', e);
    });
  }

  // ══════════════════════════════════════════
  //  Session History
  // ══════════════════════════════════════════

  function _loadSessions() {
    api('/sim/sessions?limit=10').then(function (data) {
      _sessions = data.sessions || [];
      _renderSessionList();
    }).catch(function (e) {
      console.error('[Sim] _loadSessions failed:', e);
    });
  }

  // ── Strategy name lookup for session list (matches overview.js _stratNames) ──
  var _STRAT_NAMES = {
    stable_income: {icon: '🏦', name: '稳健理财'},
    balanced:      {icon: '⚖️', name: '均衡配置'},
    growth:        {icon: '🚀', name: '积极成长'},
    sector_rotation: {icon: '🔄', name: '行业轮动'},
    value:         {icon: '💎', name: '价值投资'},
    freestyle:     {icon: '🤖', name: 'AI自由操盘'},
    conservative:  {icon: '🛡️', name: '保守型'},
    aggressive:    {icon: '🚀', name: '进取型'},
    auto:          {icon: '🤖', name: 'AI策略组合'},
  };
  var _DEFAULT_STRAT = {icon: '🤖', name: 'AI策略组合'};

  function _renderSessionList() {
    var el = $('simSessionList');
    if (!el) return;
    if (_sessions.length === 0) {
      el.innerHTML = '<div class="sim-empty">还没做过模拟，点上面的按钮开始吧！ 🚀</div>';
      return;
    }
    el.innerHTML = _sessions.map(function (s) {
      var m = s.metrics || {};
      var ret = m.total_return_pct || 0;
      var stratId = s.strategy || s.risk_level || 'auto';
      var strat = _STRAT_NAMES[stratId] || _DEFAULT_STRAT;
      return '<div class="sim-session-card" onclick="TradingApp.viewSimSession(\'' + escHtml(s.session_id) + '\')">' +
        '<div class="sim-session-top">' +
          '<span class="sim-session-period">' + escHtml(s.start_date || '') + ' → ' + escHtml(s.end_date || '') + '</span>' +
          '<span class="sim-session-ret ' + pnlClass(ret) + '">' + (ret >= 0 ? '+' : '') + fmtNum(ret, 2) + '%</span>' +
        '</div>' +
        '<div class="sim-session-kpis">' +
          '<span>' + strat.icon + ' ' + escHtml(strat.name) + '</span>' +
          '<span>回撤 ' + fmtNum(m.max_drawdown_pct || 0, 1) + '%</span>' +
          '<span>胜率 ' + fmtNum(m.win_rate || 0, 0) + '%</span>' +
          '<span>' + (s.status === 'completed' ? '✅ 已完成' : '⏳ 进行中') + '</span>' +
        '</div>' +
      '</div>';
    }).join('');
  }

  function viewSimSession(sessionId) {
    api('/sim/session/' + sessionId).then(function (data) {
      _showResults({
        session_id: sessionId,
        metrics: data.metrics || {},
        benchmark: data.benchmark || {},
        trade_count: data.trade_count || data.total_trades || 0,
        total_fees: data.total_fees || (data.metrics || {}).total_fees || 0,
      });
    }).catch(function (e) {
      console.error('[Sim] viewSimSession failed for session=' + sessionId + ':', e);
      toast('加载失败', 'error');
    });
  }

  // ══════════════════════════════════════════
  //  Navigation
  // ══════════════════════════════════════════

  function goBackToSetup() {
    // Reset all state
    _simState = 'setup';
    _fetchComplete = false;
    _fetchLogHistory = [];
    _fetchPhasesDone = {};
    _fetchPhaseProgress = {};
    _simComplete = false;
    _simResultData = null;
    _simTimelineEntries = [];
    _simTradeEntries = [];
    // Cancel any active poll timers
    _stopFetchPoll();
    _stopSimPoll();
    _stopFetchElapsed();
    // ★ Clear sessionStorage
    _clearSession();
    _showPhase('setup');
  }

  function goBackToRun() {
    _saveState('run');
    _showPhase('run');
  }

  function refreshStrategyLab() {
    _strategyLabData = null;
    _strategyLabLoading = false;
    _loadStrategyLab();
  }

  // ── Expose ──
  Object.assign(F, {
    loadSimulator: loadSimulator,
    selectBenchmark: selectBenchmark,
    setCapital: setCapital,
    startFetchData: startFetchData,
    proceedToRun: proceedToRun,
    skipFetchAndRun: skipFetchAndRun,
    startSimulation: startSimulation,
    viewSimSession: viewSimSession,
    goBackToSetup: goBackToSetup,
    goBackToRun: goBackToRun,
    setSimPeriod: setSimPeriod,
    addSymbol: addSymbol,
    removeSymbol: removeSymbol,
    clearAllSymbols: clearAllSymbols,
    onSymbolSearchInput: onSymbolSearchInput,
    addQuickGroup: addQuickGroup,
    refreshStrategyLab: refreshStrategyLab,
  });
})(window.TradingApp);
