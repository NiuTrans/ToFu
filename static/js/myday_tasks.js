/* ════════════════════════════════════
   myday_tasks.js — My Day TODO / stream mutation handlers
   Extracted from myday.js (2026-07). The task-mutation cluster: toggle/
   delete/add TODOs (incl. inherited cross-day) + stream-status cycle +
   legacy status toggle — all Api.daily.* writes. Plain window-scope
   concatenation (NOT an IIFE) — invoked at runtime from onclick handlers in
   myday.js render fns; shares the _myday state object + calls _mydayRender*
   back in myday.js. Load order is free (both before main.js).
   ════════════════════════════════════ */

/* ═══════ Toggle inherited TODO (cross-day) ═══════ */
async function _mydayToggleInheritedTodo(todoId, originDate) {
  if (!todoId || !originDate) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.today_todos) return;

  const item = cached.today_todos.find(t => t.id === todoId);
  if (!item) return;
  const newDone = !item.done;

  // Optimistic update
  item.done = newDone;
  _mydayRenderTasks(cached);

  try {
    const resp = await Api.daily.inheritedTodoToggle({ origin_date: originDate, todo_id: todoId, done: newDone });
    if (!resp || !resp.ok) {
      console.warn('[MyDay] Inherited todo toggle failed:', resp && resp.status);
      item.done = !newDone;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Inherited todo toggle error:', e);
    item.done = !newDone;
    _mydayRenderTasks(cached);
  }
}

/* ═══════ Toggle stream status (cycle: in_progress → done → blocked → in_progress) ═══════ */
async function _mydayToggleStreamStatus(streamId) {
  if (!streamId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.streams) return;

  const stream = cached.streams.find(s => s.id === streamId);
  if (!stream) return;
  const oldStatus = stream.status;
  const oldRemaining = stream.remaining;
  stream._manual = true;

  try {
    // Server owns the cycle order and returns the resolved status.
    const resp = await Api.daily.taskStatus({ date: dateStr, stream_id: streamId, action: 'cycle' });
    let body = null;
    if (resp && resp.ok) { try { body = await resp.json(); } catch (_) { body = null; } }
    if (body && body.ok && body.status) {
      stream.status = body.status;
      if (body.status === 'done') stream.remaining = null;
      try { _mydayIDB.put(dateStr, cached); } catch (e) { /* cache optional */ }
    } else {
      console.warn('[MyDay] Stream status toggle failed:', resp && resp.status);
      stream.status = oldStatus;
      stream.remaining = oldRemaining;
    }
  } catch (e) {
    console.warn('[MyDay] Stream status toggle error:', e);
    stream.status = oldStatus;
    stream.remaining = oldRemaining;
  }
  _mydayRenderTasks(cached);
  _mydayRenderCalendar();
}

/* ═══════ Toggle tomorrow TODO checkbox ═══════ */
async function _mydayToggleTodo(todoId) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tomorrow) return;

  const item = cached.tomorrow.find(t => t.id === todoId);
  if (!item) return;
  const newDone = !item.done;

  // Optimistic update
  item.done = newDone;
  _mydayRenderTasks(cached);

  try {
    const resp = await Api.daily.todoToggle({ date: dateStr, todo_id: todoId, done: newDone });
    if (!resp || !resp.ok) {
      console.warn('[MyDay] Todo toggle failed:', resp && resp.status);
      item.done = !newDone;
      _mydayRenderTasks(cached);
    } else {
      try { _mydayIDB.put(dateStr, cached); } catch (e) { /* cache optional */ }
    }
  } catch (e) {
    console.warn('[MyDay] Todo toggle error:', e);
    item.done = !newDone;
    _mydayRenderTasks(cached);
  }
}

/* ═══════ Delete a tomorrow TODO item ═══════ */
async function _mydayDeleteTodo(todoId) {
  if (!todoId) return;
  const dateStr = _myday.selectedDateStr;
  if (!dateStr) return;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tomorrow) return;

  // Optimistic removal
  const idx = cached.tomorrow.findIndex(t => t.id === todoId);
  if (idx === -1) return;
  const removed = cached.tomorrow.splice(idx, 1)[0];
  _mydayRenderTasks(cached);

  try {
    const resp = await Api.daily.taskDelete({ date: dateStr, task_id: todoId });
    if (!resp || !resp.ok) {
      console.warn('[MyDay] Delete todo failed:', resp && resp.status);
      cached.tomorrow.splice(idx, 0, removed);
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Delete todo error:', e);
    cached.tomorrow.splice(idx, 0, removed);
    _mydayRenderTasks(cached);
  }
  _mydayRenderCalendar();
}

/* ═══════ Delete an inherited TODO item (cross-day) ═══════ */
async function _mydayDeleteInheritedTodo(todoId, originDate) {
  if (!todoId || !originDate) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.today_todos) return;

  // Optimistic removal from today's inherited list
  const idx = cached.today_todos.findIndex(t => t.id === todoId);
  if (idx === -1) return;
  const removed = cached.today_todos.splice(idx, 1)[0];
  _mydayRenderTasks(cached);

  try {
    const resp = await Api.daily.inheritedTodoDelete({ origin_date: originDate, todo_id: todoId });
    if (!resp || !resp.ok) {
      console.warn('[MyDay] Delete inherited todo failed:', resp && resp.status);
      cached.today_todos.splice(idx, 0, removed);
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Delete inherited todo error:', e);
    cached.today_todos.splice(idx, 0, removed);
    _mydayRenderTasks(cached);
  }
  _mydayRenderCalendar();
}

/* ═══════ Add manual TODO task ═══════ */
async function _mydayAddTodo() {
  const input = document.getElementById('mydayTodoInput');
  if (!input) return;
  const text = input.value.trim();
  if (!text) return;
  input.value = '';
  const dateStr = _myday.selectedDateStr;
  if (!dateStr) return;

  try {
    const resp = await Api.daily.taskCreate({ date: dateStr, task: text });
    if (!resp || !resp.ok) throw new Error(`HTTP ${resp ? resp.status : 'no response'}`);
    const data = await resp.json();
    if (data.report) {
      _mydaySetCache(dateStr, data.report);
      _mydayRenderTasks(data.report);
    }
  } catch (e) {
    console.warn('[MyDay] Add task failed:', e);
  }
  _mydayRenderCalendar();
}

/* ═══════ Legacy manual todo status toggle (used by old-format reports) ═══════ */

/* ═══════ Status toggle (done ↔ incomplete) ═══════ */
async function _mydayToggleStatus(convId) {
  if (!convId) return;
  const dateStr = _myday.selectedDateStr;
  const cached = _myday.cache[dateStr];
  if (!cached || !cached.tasks) return;

  const task = cached.tasks.find(t => t.conv_id === convId || t.id === convId);
  if (!task) return;
  const oldStatus = task.status;
  const newStatus = (oldStatus === 'done') ? 'incomplete' : 'done';

  // Optimistic update
  task.status = newStatus;
  task._manual = true;
  _mydayRenderTasks(cached);

  // Persist to server
  const isTodo = convId.startsWith('todo-');
  const body = { date: dateStr, status: newStatus };
  if (isTodo) body.task_id = convId;
  else body.conv_id = convId;

  try {
    const resp = await Api.daily.taskStatus(body);
    if (!resp || !resp.ok) {
      console.warn('[MyDay] Status toggle failed:', resp && resp.status);
      task.status = oldStatus;
      _mydayRenderTasks(cached);
    }
  } catch (e) {
    console.warn('[MyDay] Status toggle error:', e);
    task.status = oldStatus;
    _mydayRenderTasks(cached);
  }

  _mydayRenderCalendar();
}
