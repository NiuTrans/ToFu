"""lib/scheduler/manager.py — ScheduledTaskManager with database persistence."""

import json
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime

from lib.log import audit_log, get_logger
from lib.scheduler.cron import cron_matches, describe_cron, next_cron_run

logger = get_logger(__name__)

# Cap on total scheduled tasks (mirrors Claude Code's MAX_JOBS). Prevents an
# LLM loop from filling the table with thousands of crons.
MAX_SCHEDULED_TASKS = 100

# Task types that execute arbitrary code and are gated by
# lib.SCHEDULER_ALLOW_CODE_EXEC. 'prompt'/'agent' are LLM-only;
# 'pg_backup'/'pg_basebackup'/'optimizer'/'reserve_reclaim' ignore their command field.
_CODE_EXEC_TASK_TYPES = frozenset({'command', 'python'})


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
        # ── Code-execution gate ──
        # task_type='command'/'python' schedule unattended arbitrary code.
        # Lock them behind the SCHEDULER_ALLOW_CODE_EXEC feature flag so a
        # deployment can disable the persistent code-exec seam entirely.
        if task_type in _CODE_EXEC_TASK_TYPES:
            import lib as _lib
            if not getattr(_lib, 'SCHEDULER_ALLOW_CODE_EXEC', True):
                raise ValueError(
                    f"task_type='{task_type}' is disabled on this deployment "
                    "(SCHEDULER_ALLOW_CODE_EXEC is off). Use task_type='prompt' "
                    "or 'agent' for LLM-driven tasks.")

        # ── Schedule validation ──
        if schedule.startswith('once:'):
            raw = schedule[5:].strip()
            try:
                target = datetime.fromisoformat(raw)
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Invalid one-time schedule '{raw}': expected "
                    f"'once:YYYY-MM-DD HH:MM'. ({e})")
            if target <= datetime.now():
                raise ValueError(
                    f"One-time schedule '{raw}' is in the past — it would "
                    "never fire. Pick a future time.")
        else:
            try:
                cron_matches(schedule)
            except ValueError as e:
                raise ValueError(f'Invalid schedule: {e}')
            # Reject crons that match no calendar date within the next year
            # (e.g. '0 0 30 2 *' — Feb 30 never exists).
            if next_cron_run(schedule) is None:
                raise ValueError(
                    f"Cron expression '{schedule}' does not match any date in "
                    "the next year — check the day-of-month / month fields.")

        # ── Capacity cap ──
        db_count = self._get_db().execute(
            'SELECT COUNT(*) AS n FROM scheduled_tasks').fetchone()
        existing = db_count['n'] if isinstance(db_count, dict) else db_count[0]
        if existing >= MAX_SCHEDULED_TASKS:
            raise ValueError(
                f'Too many scheduled tasks (max {MAX_SCHEDULED_TASKS}). '
                'Delete an existing task first.')

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
              bool(notify_on_failure), bool(notify_on_success), max_runtime, now, now,
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

        # Defense-in-depth: re-check the code-exec gate at execution time so a
        # task created while the flag was ON cannot keep running arbitrary code
        # after an operator flips SCHEDULER_ALLOW_CODE_EXEC off.
        if task_type in _CODE_EXEC_TASK_TYPES:
            import lib as _lib
            if not getattr(_lib, 'SCHEDULER_ALLOW_CODE_EXEC', True):
                logger.warning('[Scheduler] Blocked %s task "%s" — '
                               'SCHEDULER_ALLOW_CODE_EXEC is off',
                               task_type, task.get('name', '?'))
                return False, 'Blocked: code execution disabled on this deployment.'
            audit_log('scheduled_code_exec', task_id=task.get('id', '?'),
                      task_name=task.get('name', '?'), task_type=task_type,
                      command=str(command)[:500])

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

        elif task_type == 'pg_backup':
            # Scheduled PostgreSQL logical backup (pg_dumpall → data/pg_backups/).
            # ``command`` is informational only. PG-only; no-op on SQLite.
            try:
                from lib.database import backup_pg_database
                summary = backup_pg_database()
                if summary.get('ok'):
                    return True, (f"backup ok: {summary.get('path')} "
                                  f"({summary.get('size_mb')} MB, "
                                  f"pruned {summary.get('pruned', 0)})")
                reason = summary.get('reason', 'unknown')
                # 'not_pg' / 'pg_unavailable' are expected on SQLite — success.
                if reason in ('not_pg', 'pg_unavailable'):
                    return True, f'skipped ({reason})'
                return False, f'backup failed: {reason}'
            except Exception as e:
                logger.error('[Scheduler] pg_backup task failed: %s', e, exc_info=True)
                return False, 'PG backup error (see logs)'

        elif task_type == 'pg_basebackup':
            # Tier B: self-contained pg_basebackup -X stream of the local
            # primary → $TOFU_DB_BACKUP_ROOT/base/. Opt-in + split-active gated
            # inside the callee; ``command`` is informational only.
            try:
                from lib.database._bootstrap import basebackup_pg_cluster
                summary = basebackup_pg_cluster()
                if summary.get('ok'):
                    return True, f"base backup ok: {summary.get('path')}"
                reason = summary.get('reason', 'unknown')
                if reason in ('not_pg', 'tier_b_off', 'split_inactive'):
                    return True, f'skipped ({reason})'
                return False, f'base backup failed: {reason}'
            except Exception as e:
                logger.error('[Scheduler] pg_basebackup task failed: %s', e, exc_info=True)
                return False, 'PG base backup error (see logs)'

        elif task_type == 'reserve_reclaim':
            # Billing janitor: release reservations orphaned by a crash/abort
            # before settle (lib.billing.wallet_janitor.sweep_stale_reserves).
            # ``command`` is informational only. No-op when billing is inactive
            # (the sweep simply finds nothing to reclaim).
            try:
                from lib.billing.wallet_janitor import sweep_stale_reserves
                summary = sweep_stale_reserves()
                return True, (f"reclaimed {summary.get('reclaimed', 0)}/"
                              f"{summary.get('candidates', 0)} hold(s), "
                              f"{summary.get('reclaimed_micro', 0)}µ "
                              f"(errors={summary.get('errors', 0)})")
            except Exception as e:
                logger.error('[Scheduler] reserve_reclaim task failed: %s', e, exc_info=True)
                return False, 'Reserve reclaim error (see logs)'

        elif task_type == 'optimizer':
            # Daily Optimizer: runs lib.optimizer.run_once() in-process.
            # ``command`` is informational only (the handler ignores it so
            # the LLM cannot inject arbitrary code).
            try:
                import lib as _lib
                if not getattr(_lib, 'OPTIMIZER_ENABLED', True):
                    logger.info('[Scheduler] Optimizer task skipped — '
                                'OPTIMIZER_ENABLED=False')
                    return True, 'skipped (optimizer disabled in Settings)'
                from lib.optimizer import run_once
                import json as _json
                summary = run_once(dry_run=False)
                text = _json.dumps({
                    'proposals': len(summary.get('proposals', [])),
                    'applied': len(summary.get('applied', [])),
                    'pending_review': len(summary.get('pending_review', [])),
                    'rejected': len(summary.get('rejected', [])),
                    'reverts': len(summary.get('reverts', [])),
                }, ensure_ascii=False)
                return True, text
            except Exception as e:
                logger.error('[Scheduler] Optimizer task failed: %s', e, exc_info=True)
                return False, 'Optimizer execution error (see logs)'

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

        # ── Project Brain heartbeat (Pillar #5 sweep) ──
        #   After the due-task pass, dispatch any genuinely-pickable board epics
        #   on idle projects — this is what STARTS work when nothing just
        #   completed and no human is typing (incl. the cold-start first epic).
        #   Reuses THIS existing 30s tick (no new thread/global); idempotent via
        #   claim-on-dispatch + busy-guard; best-effort so a sweep failure can
        #   never break the scheduler loop.
        try:
            from lib.conversations.project_dispatch import sweep_all_active_projects
            sweep_all_active_projects()
        except Exception as e:
            logger.warning('[Scheduler] project-brain dispatch sweep skipped: %s', e)

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
            audit_log('proactive_exec_failed', task_id=task_id,
                      task_name=task.get('name', '?'), reason=str(reason)[:200])

    def _ensure_default_optimizer_task(self):
        """Idempotently register the Daily Optimizer cron task.

        Runs ``lib.optimizer.run_once()`` nightly at 03:30 local.  Matched
        by exact name so subsequent boots never create duplicates.
        """
        try:
            db = self._get_db()
            row = db.execute(
                "SELECT id FROM scheduled_tasks WHERE name=? AND task_type=?",
                ['Daily Optimizer', 'optimizer']).fetchone()
            if row:
                logger.debug('[Scheduler] Daily Optimizer task already present '
                             '(id=%s) — skipping auto-registration',
                             row['id'] if isinstance(row, dict) else row[0])
                return
            # Also tolerate an older row with the same name but wrong type
            old_row = db.execute(
                "SELECT id FROM scheduled_tasks WHERE name=?",
                ['Daily Optimizer']).fetchone()
            if old_row:
                logger.info('[Scheduler] Daily Optimizer row exists with wrong '
                            'task_type — leaving in place, not overwriting')
                return
            task = self.create_task(
                name='Daily Optimizer',
                schedule='30 3 * * *',
                command='lib.optimizer.run_once()',  # informational
                task_type='optimizer',
                description='Mines logs + daily reports once per day and applies '
                            'whitelisted optimisations (block_search_domain). '
                            'Auto-registered by lib.scheduler.manager.',
                notify_on_failure=True,
                notify_on_success=False,
                max_runtime=600,
            )
            # Respect the current feature flag on first boot.
            try:
                import lib as _lib
                if not getattr(_lib, 'OPTIMIZER_ENABLED', True):
                    self.toggle_task(task.get('id'), enabled=False)
            except Exception as _fe:
                logger.debug('[Scheduler] optimizer feature flag check skipped: %s',
                             _fe)
            logger.info('[Scheduler] Auto-registered Daily Optimizer task id=%s',
                        task.get('id'))
        except Exception as e:
            # Missing column/table here means init_db() hasn't finished the
            # scheduled_tasks migration yet. The readiness-gated caller in
            # start_scheduler_worker retries once the schema is committed, so
            # this is self-recovering — keep it at debug to avoid error.log noise.
            logger.debug('[Scheduler] Could not auto-register Daily Optimizer '
                         '(will retry after schema ready): %s', e)

    def _ensure_default_pg_backup_task(self):
        """Idempotently register the daily PostgreSQL backup cron task.

        Runs ``lib.database.backup_pg_database()`` nightly at 02:00 local
        (pg_dumpall → data/pg_backups/, with retention pruning). This is the
        durability safety net: the 2026-06-04 WAL-corruption incident was
        only recoverable because a manual dump happened to exist — this makes
        a recent dump always available. Matched by exact name so subsequent
        boots never create duplicates. No-op on the SQLite backend (the
        handler returns a benign 'skipped').
        """
        try:
            db = self._get_db()
            row = db.execute(
                "SELECT id FROM scheduled_tasks WHERE name=?",
                ['PostgreSQL Backup']).fetchone()
            if row:
                logger.debug('[Scheduler] PostgreSQL Backup task already present '
                             '— skipping auto-registration')
                return
            task = self.create_task(
                name='PostgreSQL Backup',
                schedule='0 2 * * *',
                command='lib.database.backup_pg_database()',  # informational
                task_type='pg_backup',
                description='Nightly pg_dumpall logical backup to data/pg_backups/ '
                            'with retention pruning (TOFU_PG_BACKUP_RETENTION_DAYS, '
                            'default 7). Durability safety net for crash recovery. '
                            'Auto-registered by lib.scheduler.manager.',
                notify_on_failure=True,
                notify_on_success=False,
                max_runtime=1800,
            )
            logger.info('[Scheduler] Auto-registered PostgreSQL Backup task id=%s',
                        task.get('id'))
        except Exception as e:
            logger.debug('[Scheduler] Could not auto-register PostgreSQL Backup '
                         '(will retry after schema ready): %s', e)

    def _ensure_default_pg_basebackup_task(self):
        """Idempotently register the Tier B base-backup cron task (opt-in).

        Runs ``lib.database._bootstrap.basebackup_pg_cluster()`` on a cadence
        of ``TOFU_DB_BASEBACKUP_INTERVAL_H`` (default 24h). The callee is a
        no-op unless ``TOFU_DB_TIER_B=1`` AND the local-primary split is active,
        so registering it unconditionally is safe. Bases land in
        ``$TOFU_DB_BACKUP_ROOT/base/`` as ``-X stream`` self-contained backups.
        """
        try:
            db = self._get_db()
            row = db.execute(
                "SELECT id FROM scheduled_tasks WHERE name=?",
                ['PostgreSQL Base Backup']).fetchone()
            if row:
                logger.debug('[Scheduler] PostgreSQL Base Backup task already '
                             'present — skipping auto-registration')
                return
            import os as _os
            interval_h = int(_os.environ.get('TOFU_DB_BASEBACKUP_INTERVAL_H', '24'))
            interval_h = max(1, min(interval_h, 168))
            schedule = '30 1 * * *' if interval_h == 24 else '0 */%d * * *' % interval_h
            task = self.create_task(
                name='PostgreSQL Base Backup',
                schedule=schedule,
                command='lib.database._bootstrap.basebackup_pg_cluster()',  # informational
                task_type='pg_basebackup',
                description='Tier B: pg_basebackup -X stream of the local primary '
                            'to $TOFU_DB_BACKUP_ROOT/base/ (seconds-RPO PITR base). '
                            'No-op unless TOFU_DB_TIER_B=1 + split active. '
                            'Auto-registered by lib.scheduler.manager.',
                notify_on_failure=True,
                notify_on_success=False,
                max_runtime=3600,
            )
            logger.info('[Scheduler] Auto-registered PostgreSQL Base Backup task id=%s',
                        task.get('id'))
        except Exception as e:
            logger.debug('[Scheduler] Could not auto-register PostgreSQL Base Backup '
                         '(will retry after schema ready): %s', e)

    def _ensure_default_reserve_reclaim_task(self):
        """Idempotently register the billing reserve-reclaim cron task.

        Runs ``lib.billing.wallet_janitor.sweep_stale_reserves()`` every 5
        minutes. This is the money-correctness safety net: a request that
        crashes between ``reserve(-estimate)`` and ``settle`` would otherwise
        leave the hold subtracted from the user's usable balance forever. The
        sweep releases such orphans (older than TOFU_BILLING_RESERVE_TTL,
        default 30 min) via the idempotent ``reserve_release`` path. Matched
        by exact name so subsequent boots never create duplicates. No-op when
        billing is inactive.
        """
        try:
            db = self._get_db()
            row = db.execute(
                "SELECT id FROM scheduled_tasks WHERE name=?",
                ['Billing Reserve Reclaim']).fetchone()
            if row:
                logger.debug('[Scheduler] Billing Reserve Reclaim task already '
                             'present — skipping auto-registration')
                return
            task = self.create_task(
                name='Billing Reserve Reclaim',
                schedule='*/5 * * * *',
                command='lib.billing.wallet_janitor.sweep_stale_reserves()',  # informational
                task_type='reserve_reclaim',
                description='Releases billing reservations orphaned by a crash/'
                            'abort before settle (older than '
                            'TOFU_BILLING_RESERVE_TTL, default 30 min). '
                            'Money-correctness safety net. Auto-registered by '
                            'lib.scheduler.manager.',
                notify_on_failure=True,
                notify_on_success=False,
                max_runtime=300,
            )
            logger.info('[Scheduler] Auto-registered Billing Reserve Reclaim '
                        'task id=%s', task.get('id'))
        except Exception as e:
            logger.debug('[Scheduler] Could not auto-register Billing Reserve '
                         'Reclaim (will retry after schema ready): %s', e)

    def start(self):
        """Start the background scheduler thread."""
        if self._running:
            return
        self._running = True

        # NOTE: Daily Optimizer auto-registration is deferred to the
        # readiness-gated thread in ``start_scheduler_worker`` — calling it
        # here would race ``init_db()`` (which adds scheduled_tasks columns
        # ~30s into startup) and crash with UndefinedColumn.

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
                finally:
                    # Release this long-lived thread's thread-local DB
                    # connection(s) back to the shared pool before the 30s
                    # idle sleep — otherwise the scheduler thread pins one
                    # connection per domain for the whole process lifetime
                    # (a connection-semaphore leak under high concurrency).
                    try:
                        from lib.database import close_thread_db
                        close_thread_db()
                    except Exception as _ce:
                        logger.debug('[Scheduler] close_thread_db failed: %s', _ce)
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


def start_scheduler_worker():
    """Start the background scheduler thread and resume active timers.

    Called from ``register_all`` in ``routes/__init__.py``. Spawns a
    daemon thread that polls for the ``timer_watchers`` table to be
    created (deferred since ``init_db()`` runs after route registration)
    and resumes any active timers.

    Set ``TOFU_DISABLE_SCHEDULER=1`` to skip starting the worker entirely
    — the test suite sets this so importing ``server`` (which many tests do)
    does NOT spin up a real 30s-tick scheduler + timer-resume thread that
    would run live LLM polls / web searches against the shared DB, stealing
    CPU/IO and making timing-sensitive tests flaky.
    """
    import os as _os
    if _os.environ.get('TOFU_DISABLE_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        logger.info('[Scheduler] Background worker disabled '
                    '(TOFU_DISABLE_SCHEDULER set)')
        return get_scheduler()

    mgr = get_scheduler()
    mgr.start()
    logger.info('[Scheduler] Background scheduler worker started')

    def _deferred_resume():
        from lib.database import db_available
        for attempt in range(60):
            time.sleep(2)
            if not db_available:
                continue
            try:
                from lib.database import DOMAIN_SYSTEM, get_thread_db
                db = get_thread_db(DOMAIN_SYSTEM)
                # Probe both the timer table AND the scheduled_tasks proactive
                # column — once both are visible, init_db() has committed the
                # full system schema and it's safe to auto-register / resume.
                db.execute("SELECT 1 FROM timer_watchers LIMIT 0")
                db.execute("SELECT target_conv_id FROM scheduled_tasks LIMIT 0")
                break
            except Exception:
                logger.debug('[Scheduler] system schema not ready yet '
                             '(attempt %d/60)', attempt + 1)
                continue
        else:
            logger.warning('[Scheduler] system schema not available '
                           'after 120s, skipping optimizer register + timer resume')
            return

        # Now that scheduled_tasks is fully migrated, register the Daily
        # Optimizer. Deferred here (out of mgr.start()) to avoid racing init_db.
        try:
            mgr._ensure_default_optimizer_task()
        except Exception as e:
            logger.debug('[Scheduler] default-task bootstrap failed: %s', e)

        # Register the daily PostgreSQL backup task (durability safety net).
        try:
            mgr._ensure_default_pg_backup_task()
        except Exception as e:
            logger.debug('[Scheduler] pg-backup-task bootstrap failed: %s', e)

        # Register the Tier B base-backup task (no-op unless opted in).
        try:
            mgr._ensure_default_pg_basebackup_task()
        except Exception as e:
            logger.debug('[Scheduler] pg-basebackup-task bootstrap failed: %s', e)

        # Register the billing reserve-reclaim task (money-correctness net).
        try:
            mgr._ensure_default_reserve_reclaim_task()
        except Exception as e:
            logger.debug('[Scheduler] reserve-reclaim-task bootstrap failed: %s', e)

        try:
            from lib.scheduler.timer import resume_active_timers
            resumed = resume_active_timers()
            if resumed > 0:
                logger.info('[Scheduler] Resumed %d active timer(s)', resumed)
        except Exception as e:
            logger.warning('[Scheduler] Failed to resume timers on startup: %s',
                           e)

    threading.Thread(target=_deferred_resume, name='timer-resume',
                     daemon=True).start()
    return mgr


__all__ = ['ScheduledTaskManager', 'get_scheduler', 'start_scheduler_worker']
