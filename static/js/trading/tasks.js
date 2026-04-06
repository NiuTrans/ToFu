/* ═══════════════════════════════════════════════════════════
   trading/tasks.js — Background task polling client
   
   所有长时间运行的功能（决策、回测、自我进化）通过后台任务执行，
   页面刷新、切出都不会中断。前端通过轮询获取进度和结果。
   ═══════════════════════════════════════════════════════════ */
(function (F) {
  'use strict';
  var api = F.api, toast = F.toast, $ = F._$, API = F._API;

  // ── Active pollers ──
  var _pollers = {};   // { task_id: intervalId }
  var _POLL_INTERVAL = 1500;
  // ── In-memory active task tracking (no localStorage) ──
  // ★ Migrated from localStorage to in-memory + server /tasks/active as source of truth.
  //   The server already persists active tasks, so localStorage was redundant.
  var _activeTasks = [];  // [{task_id, type, ts}]

  function _getActiveTasks() {
    return _activeTasks;
  }
  function _saveActiveTask(taskId, type) {
    _activeTasks = _activeTasks.filter(function (t) { return t.task_id !== taskId; });
    _activeTasks.push({ task_id: taskId, type: type, ts: Date.now() });
  }
  function _removeActiveTask(taskId) {
    _activeTasks = _activeTasks.filter(function (t) { return t.task_id !== taskId; });
  }
  function _clearPoller(taskId) {
    if (_pollers[taskId]) { clearInterval(_pollers[taskId]); delete _pollers[taskId]; }
  }

  // ══════════════════════════════════════════
  //  Core: submitTask / pollTask / cancelTask
  // ══════════════════════════════════════════

  /**
   * Submit a background task and start polling.
   * @param {string} type  - 'decision' | 'autopilot' | 'intel_backtest'
   * @param {object} params - task parameters
   * @param {object} callbacks - { onThinking(delta,full), onContent(delta,full), onProgress(phase), onDone(result), onError(err) }
   * @returns {Promise<string>} task_id
   */
  async function submitTask(type, params, callbacks) {
    var data = await api('/tasks/submit', {
      method: 'POST',
      body: JSON.stringify({ type: type, params: params })
    });
    if (!data || !data.task_id) throw new Error('提交失败');
    var taskId = data.task_id;
    _saveActiveTask(taskId, type);
    pollTask(taskId, callbacks);
    return taskId;
  }

  /**
   * Poll a running task for incremental output.
   * @returns {Function} stop — call to stop polling
   */
  function pollTask(taskId, callbacks) {
    var lastCursor = 0;
    var cbs = callbacks || {};
    var fullContent = '';
    var fullThinking = '';
    var stopped = false;
    var consecutiveErrors = 0;
    var MAX_CONSECUTIVE_ERRORS = 5; // Safety net: stop after 5 consecutive failures

    function _stop(reason) {
      if (stopped) return;
      stopped = true;
      _clearPoller(taskId);
      _removeActiveTask(taskId);
      console.log('[Task poll] Stopped polling ' + taskId + ': ' + reason);
    }

    function doPoll() {
      if (stopped) return;
      fetch(API + '/tasks/' + taskId + '/poll?cursor=' + lastCursor)
        .then(function (r) {
          // ★ FIX: Stop polling immediately on 404 (task gone — server restarted or expired)
          if (r.status === 404) {
            _stop('task not found (404)');
            if (cbs.onError) cbs.onError('任务已过期（服务器可能已重启），请重新提交');
            return null;
          }
          if (!r.ok) {
            consecutiveErrors++;
            if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
              _stop('too many errors (' + consecutiveErrors + ')');
              if (cbs.onError) cbs.onError('轮询失败次数过多，已停止');
            }
            return null;
          }
          consecutiveErrors = 0; // Reset on success
          return r.json();
        })
        .then(function (data) {
          if (!data || stopped) return;
          
          // ★ FIX: Check for error response body (e.g. {error: "Task not found"})
          if (data.error && !data.status) {
            _stop('server error: ' + data.error);
            if (cbs.onError) cbs.onError(data.error);
            return;
          }

          // Process new chunks
          if (data.chunks && data.chunks.length > 0) {
            data.chunks.forEach(function (chunk) {
              if (chunk.type === 'thinking') {
                fullThinking += (chunk.text || '');
                if (cbs.onThinking) cbs.onThinking(chunk.text || '', fullThinking);
              } else if (chunk.type === 'content') {
                fullContent += (chunk.text || '');
                if (cbs.onContent) cbs.onContent(chunk.text || '', fullContent);
              } else if (chunk.type === 'phase') {
                if (cbs.onProgress) cbs.onProgress(chunk.text || '');
              } else if (chunk.type === 'autopilot_result') {
                try {
                  var structured = JSON.parse(chunk.text || '{}');
                  if (cbs.onStructured) cbs.onStructured(structured);
                } catch (e) {
                  console.warn('[Task poll] Failed to parse autopilot_result chunk:', e.message);
                }
              }
            });
            lastCursor = data.cursor || lastCursor;
          }

          // Check if done
          if (data.status === 'done' || data.status === 'error' || data.status === 'cancelled') {
            _stop('task ' + data.status);
            if (data.status === 'error') {
              if (cbs.onError) cbs.onError(data.error || '未知错误');
            } else if (data.status === 'done') {
              if (cbs.onDone) cbs.onDone({ content: fullContent, thinking: fullThinking });
            }
          }
        })
        .catch(function (err) {
          consecutiveErrors++;
          if (consecutiveErrors >= MAX_CONSECUTIVE_ERRORS) {
            _stop('network errors (' + consecutiveErrors + ')');
            if (cbs.onError) cbs.onError('网络连接失败，轮询已停止');
          } else {
            console.warn('[Task poll] will retry (' + consecutiveErrors + '/' + MAX_CONSECUTIVE_ERRORS + '):', err.message);
          }
        });
    }

    // Start polling immediately
    doPoll();
    var intervalId = setInterval(doPoll, _POLL_INTERVAL);
    _pollers[taskId] = intervalId;

    // Return stop function
    return function () {
      stopped = true;
      _clearPoller(taskId);
    };
  }

  /**
   * Cancel a running task.
   */
  async function cancelTask(taskId) {
    try {
      await api('/tasks/' + taskId + '/cancel', { method: 'POST' });
      _clearPoller(taskId);
      _removeActiveTask(taskId);
    } catch (e) {
      console.warn('[Task] Cancel failed:', e.message);
    }
  }

  /**
   * Check for tasks that were running before page refresh.
   * Auto-resume polling for any still-active tasks.
   */
  async function resumeActiveTasks() {
    // ★ DB-first: always check server for active tasks on resume.
    //   In-memory _activeTasks may be empty after page refresh,
    //   so we rely on the server /tasks/active endpoint as source of truth.
    try {
      var data = await api('/tasks/active');
      var serverTaskList = data.tasks || [];
      if (serverTaskList.length === 0) return;

      serverTaskList.forEach(function (serverTask) {
        // Already polling this one in this tab? Skip.
        if (_pollers[serverTask.task_id]) return;

        var type = serverTask.type || 'decision';
        if (serverTask.status === 'running') {
          _saveActiveTask(serverTask.task_id, type);
          _showRecoveryBanner(serverTask.task_id, type, serverTask);
        } else if (serverTask.status === 'done' || serverTask.status === 'error') {
          _showCompletedBanner(serverTask.task_id, type, serverTask);
        }
      });
    } catch (e) {
      console.warn('[Tasks] Resume check failed:', e.message);
    }
  }

  function _tryFetchCompletedResult(taskId, type) {
    fetch(API + '/tasks/' + taskId + '/result')
      .then(function (r) {
        if (r.status === 404) {
          // Task gone from server — clean up in-memory tracking
          console.log('[Tasks] Task ' + taskId + ' not found on server, cleaning up');
          _removeActiveTask(taskId);
          return null;
        }
        return r.ok ? r.json() : null;
      })
      .then(function (data) {
        if (data && data.status === 'done') {
          _showCompletedBanner(taskId, type, data);
        } else if (data && data.status === 'error') {
          _showCompletedBanner(taskId, type, data);
        }
      })
      .catch(function (e) {
        console.warn('[Tasks] Failed to fetch result for ' + taskId + ':', e.message);
        _removeActiveTask(taskId);
      });
  }

  // ══════════════════════════════════════════
  //  Recovery banners
  // ══════════════════════════════════════════

  var _typeLabels = {
    'decision': 'AI决策分析',
    'autopilot': '🚀 AI自我进化',
    'intel_backtest': '🔬 情报回测'
  };

  function _showRecoveryBanner(taskId, type, serverTask) {
    var label = _typeLabels[type] || type;
    var banner = document.createElement('div');
    banner.className = 'task-recovery-banner';
    banner.id = 'task-banner-' + taskId;
    banner.innerHTML = 
      '<div class="task-banner-content">' +
        '<span class="task-banner-spinner"></span>' +
        '<span class="task-banner-text">' + label + ' 正在后台运行中...</span>' +
        '<button class="task-banner-btn view" onclick="TradingApp.taskViewRunning(\'' + taskId + '\',\'' + type + '\')">查看进度</button>' +
        '<button class="task-banner-btn cancel" onclick="TradingApp.taskCancelRunning(\'' + taskId + '\')">取消</button>' +
      '</div>';
    
    _insertBanner(banner);
  }

  function _showCompletedBanner(taskId, type, serverTask) {
    var label = _typeLabels[type] || type;
    var isError = serverTask.status === 'error';
    var banner = document.createElement('div');
    banner.className = 'task-recovery-banner ' + (isError ? 'error' : 'done');
    banner.id = 'task-banner-' + taskId;
    banner.innerHTML = 
      '<div class="task-banner-content">' +
        '<span>' + (isError ? '❌' : '✅') + '</span>' +
        '<span class="task-banner-text">' + label + (isError ? ' 执行失败' : ' 已完成') + '</span>' +
        (isError ? '' : '<button class="task-banner-btn view" onclick="TradingApp.taskViewResult(\'' + taskId + '\',\'' + type + '\')">查看结果</button>') +
        '<button class="task-banner-btn dismiss" onclick="this.closest(\'.task-recovery-banner\').remove()">关闭</button>' +
      '</div>';

    _insertBanner(banner);
  }

  function _insertBanner(banner) {
    // Insert at top of main content area
    var container = document.getElementById('task-banners');
    if (!container) {
      var wrap = document.createElement('div');
      wrap.id = 'task-banners';
      wrap.style.cssText = 'position:fixed;top:60px;right:20px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:420px;';
      document.body.appendChild(wrap);
      container = wrap;
    }
    container.appendChild(banner);
    // Auto-dismiss completed banners after 30s
    if (banner.classList.contains('done') || banner.classList.contains('error')) {
      setTimeout(function () { if (banner.parentNode) banner.remove(); }, 30000);
    }
  }

  function _removeBanner(taskId) {
    var el = document.getElementById('task-banner-' + taskId);
    if (el) el.remove();
  }

  // ── Banner action handlers ──

  function taskViewRunning(taskId, type) {
    _removeBanner(taskId);
    // Navigate to the appropriate user-facing page
    if (type === 'decision' || type === 'autopilot') {
      F.navigate('brain');
    } else if (type === 'intel_backtest') {
      F.navigate('simulator');
    } else {
      F.navigate('overview');
    }
    toast('后台任务运行中，请稍候...', 'info');
  }

  function taskViewResult(taskId, type) {
    _removeBanner(taskId);
    // Navigate to user-facing page
    if (type === 'decision' || type === 'autopilot') {
      F.navigate('brain');
    } else if (type === 'intel_backtest') {
      F.navigate('simulator');
    } else {
      F.navigate('overview');
    }
    toast('任务已完成', 'success');
  }

  function taskCancelRunning(taskId) {
    cancelTask(taskId);
    _removeBanner(taskId);
    toast('任务已取消', 'info');
  }

  // ══════════════════════════════════════════
  //  Type-specific resume/render helpers
  // ══════════════════════════════════════════

  function _resumeDecisionPolling(taskId) {
    var content = $('brainContent');
    var thinking = $('brainThinking');
    var btn = $('brainAnalyzeBtn');

    if (content) content.innerHTML = '<div style="text-align:center;padding:20px"><span class="spinner spinner-lg"></span><p style="margin-top:8px;color:var(--t3)">后台任务运行中，正在恢复进度...</p></div>';
    if (btn) { btn.disabled = true; btn.textContent = '🔄 后台运行中...'; }
    if (thinking) { thinking.style.display = 'none'; thinking.innerHTML = ''; }

    pollTask(taskId, {
      onThinking: function (delta, full) {
        if (thinking) { thinking.style.display = 'block'; thinking.textContent = full; }
      },
      onContent: function (delta, full) {
        if (content) content.innerHTML = F.renderMarkdown(full);
      },
      onDone: function (result) {
        if (btn) { btn.disabled = false; btn.textContent = '开始分析'; }
        toast('AI分析完成', 'success');
        if (typeof F.loadHistory === 'function') F.loadHistory();
        if (typeof F.loadBrainState === 'function') F.loadBrainState();
      },
      onError: function (err) {
        if (content) content.innerHTML = '<div class="error-msg">❌ ' + F.escHtml(err) + '</div>';
        if (btn) { btn.disabled = false; btn.textContent = '开始分析'; }
      }
    });
  }

  function _renderDecisionResult(data) {
    var content = $('brainContent');
    var thinking = $('brainThinking');

    if (data.thinking && thinking) {
      thinking.style.display = 'block';
      thinking.textContent = data.thinking;
    }
    if (data.result && content) {
      content.innerHTML = F.renderMarkdown(data.result);
    }
  }

  function _resumeAutopilotPolling(taskId) {
    var contentEl = $('brainContent');
    var thinkingEl = $('brainThinking');
    var btn = $('brainAnalyzeBtn');
    var fullThinking = '';

    if (btn) { btn.disabled = true; btn.textContent = '🔄 后台运行中...'; }
    if (contentEl) contentEl.innerHTML = '<div style="text-align:center;padding:20px"><span class="spinner spinner-lg"></span><p style="margin-top:8px;color:var(--t3)">后台任务运行中，正在恢复进度...</p></div>';
    if (thinkingEl) { thinkingEl.style.display = 'none'; thinkingEl.innerHTML = ''; }

    // Helper to strip <autopilot_result> JSON from rendered markdown
    function _stripApResult(text) {
      return (text || '').replace(/<autopilot_result>[\s\S]*?<\/autopilot_result>/g, '').trim();
    }

    pollTask(taskId, {
      onThinking: function (delta, full) {
        fullThinking = full;
        if (thinkingEl) { thinkingEl.style.display = 'block'; thinkingEl.textContent = full; }
      },
      onContent: function (delta, full) {
        if (contentEl) contentEl.innerHTML = F.renderMarkdown(_stripApResult(full));
      },
      onProgress: function (phase) {
        toast(phase, 'info');
      },
      onStructured: function (data) {
        if (typeof F.renderBrainStructuredResult === 'function') F.renderBrainStructuredResult(data);
      },
      onDone: function (result) {
        if (btn) { btn.disabled = false; btn.textContent = '开始分析'; }
        // Fallback: try client-side extraction if structured panels still empty
        if (result && result.content) {
          var structuredEl = $('brainStructuredResult');
          var alreadyRendered = structuredEl && structuredEl.innerHTML.trim().length > 0;
          if (!alreadyRendered) {
            var match = result.content.match(/<autopilot_result>\s*([\s\S]*?)\s*<\/autopilot_result>/);
            if (match) {
              try {
                var parsed = JSON.parse(match[1]);
                if (typeof F.renderBrainStructuredResult === 'function') {
                  F.renderBrainStructuredResult({
                    recommendations: parsed.position_recommendations || parsed.recommendations || [],
                    risk_factors: parsed.risk_factors || [],
                    strategy_updates: parsed.strategy_updates || [],
                    market_outlook: parsed.market_outlook || '',
                    confidence_score: parsed.confidence_score || 0,
                    next_review: parsed.next_review || '',
                  });
                }
              } catch (e) { console.warn('[resume] client-side structured extraction failed:', e.message); }
            }
          }
        }
        toast('自我进化周期完成', 'success');
        if (typeof F.loadBrainState === 'function') F.loadBrainState();
      },
      onError: function (err) {
        if (btn) { btn.disabled = false; btn.textContent = '开始分析'; }
        if (contentEl) contentEl.innerHTML = '<div class="error-msg">❌ ' + F.escHtml(err) + '</div>';
      }
    });
  }

  function _renderAutopilotResult(data) {
    var contentEl = $('brainContent');
    if (data.result && contentEl) {
      var cleaned = (data.result || '').replace(/<autopilot_result>[\s\S]*?<\/autopilot_result>/g, '').trim();
      contentEl.innerHTML = F.renderMarkdown(cleaned);
      // Try to render structured data from the result
      var match = data.result.match(/<autopilot_result>\s*([\s\S]*?)\s*<\/autopilot_result>/);
      if (match) {
        try {
          var parsed = JSON.parse(match[1]);
          if (typeof F.renderBrainStructuredResult === 'function') {
            F.renderBrainStructuredResult({
              recommendations: parsed.position_recommendations || parsed.recommendations || [],
              risk_factors: parsed.risk_factors || [],
              strategy_updates: parsed.strategy_updates || [],
              market_outlook: parsed.market_outlook || '',
              confidence_score: parsed.confidence_score || 0,
              next_review: parsed.next_review || '',
            });
          }
        } catch (e) { console.warn('[renderAutopilotResult] structured extraction failed:', e.message); }
      }
    }
  }

  function _resumeIntelBacktestPolling(taskId) {
    var content = $('btIntelContent') || $('btAnalysis');
    var thinking = $('btIntelThinking');

    if (content) content.innerHTML = '<div style="text-align:center;padding:20px"><span class="spinner spinner-lg"></span><p>后台回测运行中...</p></div>';

    pollTask(taskId, {
      onThinking: function (delta, full) {
        if (thinking) { thinking.style.display = 'block'; thinking.textContent = full; }
      },
      onContent: function (delta, full) {
        if (content) content.innerHTML = F.renderMarkdown(full);
      },
      onDone: function (result) {
        toast('情报回测完成', 'success');
      },
      onError: function (err) {
        if (content) content.innerHTML = '<div class="error-msg">❌ ' + F.escHtml(err) + '</div>';
      }
    });
  }

  function _renderIntelBacktestResult(data) {
    var content = $('btIntelContent') || $('btAnalysis');
    if (data.result && content) content.innerHTML = F.renderMarkdown(data.result);
  }

  // ══════════════════════════════════════════
  //  Exports
  // ══════════════════════════════════════════

  Object.assign(F, {
    submitTask: submitTask,
    pollTask: pollTask,
    cancelTask: cancelTask,
    resumeActiveTasks: resumeActiveTasks,
    taskViewRunning: taskViewRunning,
    taskViewResult: taskViewResult,
    taskCancelRunning: taskCancelRunning,
    // Internal, used by other modules
    _resumeDecisionPolling: _resumeDecisionPolling,
    _resumeAutopilotPolling: _resumeAutopilotPolling,
    _resumeIntelBacktestPolling: _resumeIntelBacktestPolling,
  });

})(window.TradingApp);
