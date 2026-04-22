"""lib/scheduler/manager.py — ScheduledTaskManager with database persistence."""

import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from lib.log import get_logger
from lib.scheduler.cron import cron_matches, describe_cron, next_cron_run

logger = get_logger(__name__)


class ScheduledTaskManager:
    """Manages scheduled tasks with database persistence."""

    def __init__(self, db_path=None):
        self.db_path = db_path  # kept for compat, not used with PG
        self._init_table()
        self._running = False
        self._thread = None
        self._execution_log = []  # Recent execution log (in-memory)
        self._log_lock = threading.Lock()  # protects _execution_log

    def _get_db(self):
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        return get_thread_db(DOMAIN_SYSTEM)

    def _init_table(self):
        db = self._get_db()
        db.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                schedule TEXT NOT NULL,
                task_type TEXT NOT NULL DEFAULT 'command',
                command TEXT NOT NULL,
                description TEXT DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                notify_on_failure BOOLEAN NOT NULL DEFAULT TRUE,
                notify_on_success BOOLEAN NOT NULL DEFAULT FALSE,
                max_runtime INTEGER NOT NULL DEFAULT 300,
                last_run TEXT,
                last_result TEXT,
                last_status TEXT DEFAULT 'never',
                run_count INTEGER NOT NULL DEFAULT 0,
                fail_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        db.commit()

    def create_task(self, name, schedule, command, task_type='command',
                    description='', notify_on_failure=True, notify_on_success=False,
                    max_runtime=300, target_conv_id='', source_conv_id='',
                    tools_config=None, max_executions=0, expires_at=''):
        """Create a new scheduled task.

        Args:
            name: Human-readable task name
            schedule: Cron expression ('*/5 * * * *') or 'once:YYYY-MM-DD HH:MM'
            command: Shell command, Python code, LLM prompt, or agent instruction
            task_type: 'command' (shell), 'python' (Python code), 'prompt' (LLM),
                       'agent' (proactive agentic task with tools + SSE)
            description: What this task does
            notify_on_failure: Send notification on failure
            notify_on_success: Send notification on success
            max_runtime: Max seconds before killing (not used for 'agent')
            target_conv_id: Conversation to execute in (agent only)
            source_conv_id: Conversation where this was created (agent only)
            tools_config: Dict of tool settings for agent execution
            max_executions: Auto-disable after this many executions (0=unlimited)
            expires_at: Auto-disable after this ISO datetime

        Returns:
            task dict
        """
        # Validate cron expression
        if not schedule.startswith('once:'):
            try:
                cron_matches(schedule)
            except ValueError as e:
                raise ValueError(f'Invalid schedule: {e}')

        task_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        tools_json = json.dumps(tools_config or {}, ensure_ascii=False)

        db = self._get_db()
        db.execute('''
            INSERT INTO scheduled_tasks
            (id, name, schedule, task_type, command, description,
             notify_on_failure, notify_on_success, max_runtime, created_at, updated_at,
             target_conv_id, source_conv_id, tools_config, max_executions, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', [task_id, name, schedule, task_type, command, description,
              int(notify_on_failure), int(notify_on_success), max_runtime, now, now,
              target_conv_id or '', source_conv_id or '', tools_json,
              max_executions, expires_at or ''])
        db.commit()

        task = dict(db.execute('SELECT * FROM scheduled_tasks WHERE id=?', [task_id]).fetchone())

        logger.info('✅ Created task "%s" (id=%s, type=%s, schedule=%s, target_conv=%s)',
                    name, task_id, task_type, schedule, target_conv_id or 'N/A')
        return task

    def list_tasks(self, include_disabled=False):
        """List all tasks."""
        db = self._get_db()
        if include_disabled:
            rows = db.execute('SELECT * FROM scheduled_tasks ORDER BY created_at DESC').fetchall()
        else:
            rows = db.execute('SELECT * FROM scheduled_tasks WHERE enabled=TRUE ORDER BY created_at DESC').fetchall()

        tasks = []
        for r in rows:
            t = dict(r)
            # Add next run time
            if not t['schedule'].startswith('once:') and t['enabled']:
                try:
                    nxt = next_cron_run(t['schedule'])
                    t['next_run'] = nxt.isoformat() if nxt else None
                except Exception as e:
                    logger.debug('[Scheduler] next_cron_run parse failed for task %s schedule=%s: %s',
                                t.get('id', '?'), t.get('schedule', '?'), e, exc_info=True)
                    t['next_run'] = None
            else:
                t['next_run'] = None
            t['schedule_human'] = describe_cron(t['schedule']) if not t['schedule'].startswith('once:') else f"once at {t['schedule'][5:]}"
            tasks.append(t)

        return tasks

    def get_task(self, task_id):
        """Get a single task by ID."""
        db = self._get_db()
        row = db.execute('SELECT * FROM scheduled_tasks WHERE id=?', [task_id]).fetchone()
        return dict(row) if row else None

    def update_task(self, task_id, **kwargs):
        """Update task fields."""
        allowed = {'name', 'schedule', 'command', 'task_type', 'description',
                   'enabled', 'notify_on_failure', 'notify_on_success', 'max_runtime',
                   'target_conv_id', 'source_conv_id', 'tools_config',
                   'poll_count', 'last_poll_at', 'last_poll_decision', 'last_poll_reason',
                   'last_execution_at', 'last_execution_task_id', 'last_execution_status',
                   'execution_count', 'max_executions', 'expires_at'}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        updates['updated_at'] = datetime.now().isoformat()

        db = self._get_db()
        set_clause = ', '.join(f'{k}=?' for k in updates)
        db.execute(f'UPDATE scheduled_tasks SET {set_clause} WHERE id=?',
                   list(updates.values()) + [task_id])
        db.commit()
        return True

    def delete_task(self, task_id):
        """Delete a task."""
        db = self._get_db()
        db.execute('DELETE FROM scheduled_tasks WHERE id=?', [task_id])
        db.commit()
        logger.info('🗑️ Deleted task %s', task_id)
        return True

    def toggle_task(self, task_id, enabled=None):
        """Enable or disable a task."""
        db = self._get_db()
        if enabled is None:
            row = db.execute('SELECT enabled FROM scheduled_tasks WHERE id=?', [task_id]).fetchone()
            if not row:
                return None
            enabled = not row['enabled']

        db.execute('UPDATE scheduled_tasks SET enabled=?, updated_at=? WHERE id=?',
                   [bool(enabled), datetime.now().isoformat(), task_id])
        db.commit()
        return enabled

    def get_execution_log(self, limit=20):
        """Get recent execution log."""
        with self._log_lock:
            return list(self._execution_log[-limit:])

    # ── Task Execution ──

    def _execute_task(self, task):
        """Execute a single task. Returns (success, result_text)."""
        task_type = task['task_type']
        command = task['command']
        max_runtime = task.get('max_runtime', 300)

        logger.info('[Scheduler] Executing task type=%s cmd=%s', task_type, str(command)[:100])

        if task_type == 'command':
            try:
                from lib.compat import get_shell_args
                result = subprocess.run(
                    get_shell_args(command),
                    capture_output=True, text=True,
                    timeout=max_runtime,
                )
                output = result.stdout[:50000]
                if result.stderr:
                    output += f'\n[stderr] {result.stderr[:10000]}'
                success = result.returncode == 0
                return success, output if output.strip() else f'(exit code: {result.returncode})'
            except subprocess.TimeoutExpired:
                logger.warning('[Scheduler] Command task timed out after %ds: cmd=%s', max_runtime, str(command)[:100], exc_info=True)
                return False, f'Timed out after {max_runtime}s'
            except Exception as e:
                logger.error('[Scheduler] Command task failed: cmd=%s: %s', str(command)[:100], e, exc_info=True)
                return False, 'Command execution error (see logs)'

        elif task_type == 'python':
            try:
                result = subprocess.run(
                    [sys.executable, '-c', command],
                    capture_output=True, text=True,
                    timeout=max_runtime,
                )
                output = result.stdout[:50000]
                if result.stderr:
                    output += f'\n[stderr] {result.stderr[:10000]}'
                return result.returncode == 0, output or f'(exit code: {result.returncode})'
            except subprocess.TimeoutExpired:
                logger.warning('[Scheduler] Python task timed out after %ds: cmd=%s', max_runtime, str(command)[:100], exc_info=True)
                return False, f'Timed out after {max_runtime}s'
            except Exception as e:
                logger.error('[Scheduler] Python task failed: cmd=%s: %s', str(command)[:100], e, exc_info=True)
                return False, 'Python execution error (see logs)'

        elif task_type == 'prompt':
            # Use LLM to answer a prompt — useful for periodic analysis
            try:
                from lib.llm_dispatch import smart_chat
                content, usage = smart_chat(
                    messages=[{'role': 'user', 'content': command}],
                    max_tokens=4096,
                    log_prefix='[Scheduler]',
                )
                return True, content
            except Exception as e:
                logger.error('[Scheduler] Prompt task failed: cmd=%s: %s', str(command)[:100], e, exc_info=True)
                return False, 'Prompt execution error (see logs)'

        return False, f'Unknown task type: {task_type}'

    def run_task_now(self, task_id):
        """Manually trigger a task immediately."""
        task = self.get_task(task_id)
        if not task:
            return None, 'Task not found'

        logger.info('▶️ Running task "%s" (manual trigger)', task['name'])
        success, result = self._execute_task(task)

        now = datetime.now().isoformat()
        db = self._get_db()
        db.execute('''
            UPDATE scheduled_tasks
            SET last_run=?, last_result=?, last_status=?, run_count=run_count+1,
                fail_count=fail_count+? , updated_at=?
            WHERE id=?
        ''', [now, result[:10000], 'ok' if success else 'failed', 0 if success else 1, now, task_id])
        db.commit()

        status = '✅' if success else '❌'
        logger.info('%s Task "%s" → %s', status, task['name'], result[:200])

        with self._log_lock:
            self._execution_log.append({
                'task_id': task_id,
                'task_name': task['name'],
                'time': now,
                'success': success,
                'result': result[:2000],
            })
            # Keep log bounded
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]

        return success, result

    # ── Background Scheduler ──

    def _check_and_run_due_tasks(self):
        """Check all tasks and run any that are due."""
        now = datetime.now()
        db = self._get_db()
        tasks = db.execute('SELECT * FROM scheduled_tasks WHERE enabled=TRUE').fetchall()

        for task in tasks:
            task = dict(task)
            schedule = task['schedule']

            # One-time tasks
            if schedule.startswith('once:'):
                target_time = datetime.fromisoformat(schedule[5:].strip())
                if now >= target_time:
                    # Check if already run
                    if task['run_count'] > 0:
                        continue
                    self._run_and_record(task)
                    # Auto-disable after one-time run
                    self.toggle_task(task['id'], enabled=False)
                continue

            # Cron tasks
            try:
                if not cron_matches(schedule, now):
                    continue
            except ValueError:
                logger.debug('[Scheduler] invalid cron expression for task %s: %s',
                            task.get('id', '?'), schedule, exc_info=True)
                continue
            # Prevent double-run within the same minute
            last_run_field = task.get('last_poll_at') if task['task_type'] == 'agent' else task['last_run']
            if last_run_field:
                try:
                    last = datetime.fromisoformat(last_run_field)
                    if (now - last).total_seconds() < 55:
                        continue
                except Exception as e:
                    logger.warning('[Scheduler] task %s last_run timestamp parse failed: %s: %s',
                                  task.get('id', '?'), last_run_field, e, exc_info=True)

            # Route: agent tasks use proactive poll→execute, others use direct execution
            if task['task_type'] == 'agent':
                self._run_proactive_poll(task)
            else:
                self._run_and_record(task)

    def _run_and_record(self, task):
        """Run task and record result in DB."""
        task_id = task['id']
        logger.info('▶️ Running scheduled task "%s"', task['name'])

        success, result = self._execute_task(task)

        now = datetime.now().isoformat()
        db = self._get_db()
        db.execute('''
            UPDATE scheduled_tasks
            SET last_run=?, last_result=?, last_status=?, run_count=run_count+1,
                fail_count=fail_count+?, updated_at=?
            WHERE id=?
        ''', [now, result[:10000], 'ok' if success else 'failed', 0 if success else 1, now, task_id])
        db.commit()

        status = '✅' if success else '❌'
        logger.info('%s "%s" → %s', status, task['name'], result[:200])

        with self._log_lock:
            self._execution_log.append({
                'task_id': task_id,
                'task_name': task['name'],
                'time': now,
                'success': success,
                'result': result[:2000],
            })
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]

    def _run_proactive_poll(self, task):
        """Run the proactive agent poll→decide→execute cycle for a task_type='agent'.

        Phase B: Lightweight LLM poll (cheap model, no tools, independent context).
        Phase C: If poll says act=true, create full agentic task in target conversation.
        """
        from lib.scheduler.proactive import (
            execute_proactive_task,
            gather_system_status,
            is_task_executing,
            poll_decision,
            record_poll,
            should_auto_disable,
        )

        task_id = task['id']
        pfx = f'[Proactive:{task_id[:8]}]'

        # ── Pre-checks ──
        if should_auto_disable(task):
            self.update_task(task_id, enabled=False)
            logger.info('%s Auto-disabled (max_executions or expired)', pfx)
            return

        if is_task_executing(task):
            logger.debug('%s Skipping poll — previous execution still running '
                         '(task_id=%s)', pfx, task.get('last_execution_task_id', '?')[:8])
            return

        # ── Phase B: Poll ──
        logger.info('%s Starting poll #%d', pfx, task.get('poll_count', 0) + 1)
        status_snapshot = gather_system_status(task)
        should_act, reason, tokens_used = poll_decision(task)

        decision = 'act' if should_act else 'skip'
        now = datetime.now().isoformat()

        # Update task poll state in DB
        db = self._get_db()
        db.execute('''
            UPDATE scheduled_tasks
            SET poll_count=poll_count+1, last_poll_at=?, last_poll_decision=?,
                last_poll_reason=?, last_run=?, updated_at=?
            WHERE id=?
        ''', [now, decision, reason[:500], now, now, task_id])
        db.commit()

        logger.info('%s Poll decision: %s — reason: %s (tokens=%d)',
                    pfx, decision, reason[:100], tokens_used)

        if not should_act:
            record_poll(task_id, 'skip', reason, 'cheap', tokens_used, status_snapshot)
            return

        # ── Phase C: Execute ──
        exec_task_id = execute_proactive_task(task)

        if exec_task_id:
            # Update execution state
            db.execute('''
                UPDATE scheduled_tasks
                SET last_execution_at=?, last_execution_task_id=?,
                    last_execution_status='running', execution_count=execution_count+1,
                    updated_at=?
                WHERE id=?
            ''', [now, exec_task_id, now, task_id])
            db.commit()

            record_poll(task_id, 'act', reason, 'cheap', tokens_used,
                       status_snapshot, execution_task_id=exec_task_id)
            logger.info('%s 🚀 Execution started: agentic_task=%s', pfx, exec_task_id[:8])
        else:
            record_poll(task_id, 'act_failed', reason, 'cheap', tokens_used, status_snapshot)
            logger.error('%s ❌ Execution failed to start', pfx)

    def start(self):
        """Start the background scheduler thread."""
        if self._running:
            return
        self._running = True

        def _loop():
            logger.info('🕐 Background scheduler started')
            while self._running:
                try:
                    self._check_and_run_due_tasks()
                except Exception as e:
                    # Transient DB connection errors (PG timeout, connection
                    # reset) are routinely recoverable on the next 30s tick —
                    # downgrade to WARNING without a traceback so they don't
                    # pollute error.log. Anything else is still a real bug.
                    etype = type(e).__name__
                    _msg_lower = str(e).lower()
                    is_transient_db = (
                        etype in ('OperationalError', 'InterfaceError')
                        or 'timeout expired' in _msg_lower
                        or 'connection to server' in _msg_lower
                        or 'database is locked' in _msg_lower
                    )
                    if is_transient_db:
                        logger.warning('[Scheduler] Transient DB error in check loop '
                                       '(will retry in 30s): %s: %s', etype, e)
                    else:
                        logger.error('[Scheduler] Error in scheduler check loop: %s',
                                     e, exc_info=True)
                time.sleep(30)  # Check every 30 seconds

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background scheduler."""
        self._running = False
        logger.info('Stopped')


# ── Singleton ──

_manager = None
_manager_lock = threading.Lock()


def get_scheduler():
    """Get or create the singleton ScheduledTaskManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = ScheduledTaskManager()
    return _manager


__all__ = ['ScheduledTaskManager', 'get_scheduler']
