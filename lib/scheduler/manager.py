"""Owner-scoped task scheduling backed by the storage Sidecar.

The manager owns cron evaluation and execution dispatch. Durable task identity,
claims, and results are semantic Sidecar operations; backend maintenance is
invoked through the same boundary rather than through database-specific jobs.
"""

import json
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta

from lib.identity import PrincipalContext
from lib.log import audit_log, get_logger
from lib.scheduler.contract import DUE_CLAIM_INTERVAL_SECONDS
from lib.scheduler.cron import cron_matches, describe_cron, next_cron_run
from lib.scheduler.process_runner import run_bounded_process

logger = get_logger(__name__)


def _scheduler_client(*, write: bool = False):
    from lib.storage import get_storage_client

    return get_storage_client(write=write)


def _scheduler_wire_task(task):
    """Keep the historical manager contract (tools_config is JSON text)."""
    task = dict(task)
    if isinstance(task.get("tools_config"), (dict, list)):
        task["tools_config"] = json.dumps(task["tools_config"], ensure_ascii=False)
    return task


def _task_owner_user_id(task: dict) -> int:
    try:
        owner_user_id = int(task['user_id'])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('scheduled task is missing a numeric owner') from exc
    if owner_user_id < 1:
        raise ValueError('scheduled task owner must be positive')
    return owner_user_id


def _task_process_principal(
    task: dict, *, system_scope: str | None = None
) -> PrincipalContext:
    """Build the explicit least-privilege identity for one child process."""
    owner_user_id = _task_owner_user_id(task)
    if system_scope is not None:
        return PrincipalContext.system(
            subject_id=f'scheduler.{system_scope}',
            owner_user_id=owner_user_id,
            scopes={system_scope},
        )
    return PrincipalContext.user(
        subject_id=f'scheduled-user:{owner_user_id}',
        owner_user_id=owner_user_id,
        scopes={'agents:scheduler'},
    )


def _validated_scheduler_process_principal(
    principal: PrincipalContext,
) -> PrincipalContext:
    """Accept only the least-privilege system identity for the scheduler."""
    if not isinstance(principal, PrincipalContext):
        raise TypeError('scheduler worker requires PrincipalContext')
    if principal.kind != 'system':
        raise PermissionError('scheduler worker requires a system principal')
    principal.require_scope('scheduler:run')
    return principal


# Task types that execute arbitrary code and are gated by
# lib.SCHEDULER_ALLOW_CODE_EXEC. 'prompt'/'agent' are LLM-only;
# System task types ignore their informational command field.
_CODE_EXEC_TASK_TYPES = frozenset({"command", "python"})
_ASYNC_TASK_TYPES = frozenset({'daily_report_backfill', 'storage_backup'})


def _application_managed_storage_backups_enabled() -> bool:
    """Personal storage is app-owned; distributed PostgreSQL is platform-owned."""
    from runtime_guards import resolve_deployment_mode

    try:
        return resolve_deployment_mode() == 'personal'
    except RuntimeError as exc:
        logger.error(
            '[Scheduler] Database backup mode is invalid; disabling '
            'application-managed backups: %s',
            exc,
        )
        return False


def _run_daily_report_backfill(task: dict) -> tuple[bool, str]:
    """Execute the typed report action with only its row owner's authority."""
    try:
        from lib.daily_report import _backfill_yesterday_if_missing

        summary = _backfill_yesterday_if_missing(
            principal=_task_process_principal(
                task, system_scope='reports:maintain'),
        )
        return bool(summary.get('ok')), json.dumps(
            summary, ensure_ascii=False, sort_keys=True)
    except Exception as e:
        logger.error(
            '[Scheduler] daily report backfill failed: %s',
            e, exc_info=True)
        return False, 'Daily report backfill error (see logs)'


class ScheduledTaskManager:
    """Manages scheduled tasks with database persistence."""

    def __init__(self):
        self._running = False
        self._stop_event = threading.Event()
        self._thread = None
        self._execution_log = []  # Recent execution log (in-memory)
        self._log_lock = threading.Lock()  # protects _execution_log
        self._maintenance_lock = threading.Lock()
        self._maintenance_threads: dict[str, threading.Thread] = {}
        self._startup_task_ids: set[str] = set()
        self._process_principal: PrincipalContext | None = None

    def _background_principal(self) -> PrincipalContext:
        principal = getattr(self, '_process_principal', None)
        if not isinstance(principal, PrincipalContext):
            raise RuntimeError('scheduler worker has no process principal')
        return _validated_scheduler_process_principal(principal)

    def _ensure_default_task(
        self,
        *,
        system_key: str,
        name: str,
        schedule: str,
        command: str,
        task_type: str,
        description: str,
        max_runtime: int,
        enabled: bool = True,
        reconcile_enabled: bool = False,
    ) -> tuple[dict, bool]:
        """Atomically install one built-in task for the composed owner."""
        owner_user_id = self._background_principal().require_owner(
            context='built-in scheduler task')
        now = datetime.now().isoformat()
        result = _scheduler_client(write=True).command(
            "scheduler.task.ensure",
            {
                "system_key": system_key,
                "task_id": f"system-{owner_user_id}-{system_key}",
                "user_id": owner_user_id,
                "name": name,
                "schedule": schedule,
                "task_type": task_type,
                "command": command,
                "description": description,
                "enabled": bool(enabled),
                "reconcile_enabled": bool(reconcile_enabled),
                "notify_on_failure": True,
                "notify_on_success": False,
                "max_runtime": max_runtime,
                "created_at": now,
                "updated_at": now,
                "tools_config": {},
                "condition_kind": "llm",
            },
            f"scheduler.ensure:{system_key}:{uuid.uuid4().hex}",
        )
        return _scheduler_wire_task(result["task"]), bool(result["created"])

    @staticmethod
    def _claim_due_task(task: dict, now: datetime) -> bool:
        """Claim one scheduled execution before any external side effect."""
        task_id = str(task["id"])
        now_text = now.isoformat()
        result = _scheduler_client(write=True).command(
            "scheduler.task.claim_due",
            {
                "task_id": task_id,
                "user_id": int(task["user_id"]),
                "lane": "poll" if task["task_type"] == "agent" else "run",
                "now": now_text,
                "minimum_interval_seconds": DUE_CLAIM_INTERVAL_SECONDS,
            },
            f"scheduler.claim:{task_id}:{now_text}",
        )
        return bool(result.get("claimed"))

    def create_task(
        self,
        name,
        schedule,
        command,
        *,
        principal: PrincipalContext,
        task_type="command",
        description="",
        notify_on_failure=True,
        notify_on_success=False,
        max_runtime=300,
        target_conv_id="",
        source_conv_id="",
        tools_config=None,
        max_executions=0,
        expires_at="",
        condition_command="",
        condition_regex="",
    ):
        """Create a new scheduled task.

        Args:
            principal: Authenticated owner authorizing the standing task
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
        if not isinstance(principal, PrincipalContext):
            raise TypeError('scheduled task creation requires PrincipalContext')
        user_id = principal.require_owner(context='scheduled task creation')
        if not principal.has_scope('agents:scheduler'):
            raise PermissionError(
                'scheduled task creation requires agents:scheduler scope')

        # ── Code-execution gate ──
        # task_type='command'/'python' schedule unattended arbitrary code.
        # Lock them behind the SCHEDULER_ALLOW_CODE_EXEC feature flag so a
        # deployment can disable the persistent code-exec seam entirely.
        if task_type in _CODE_EXEC_TASK_TYPES:
            import lib as _lib

            if not getattr(_lib, "SCHEDULER_ALLOW_CODE_EXEC", True):
                raise ValueError(
                    f"task_type='{task_type}' is disabled on this deployment "
                    "(SCHEDULER_ALLOW_CODE_EXEC is off). Use task_type='prompt' "
                    "or 'agent' for LLM-driven tasks."
                )

        # ── Schedule validation ──
        if schedule.startswith("once:"):
            raw = schedule[5:].strip()
            try:
                target = datetime.fromisoformat(raw)
            except (ValueError, TypeError) as e:
                raise ValueError(
                    f"Invalid one-time schedule '{raw}': expected "
                    f"'once:YYYY-MM-DD HH:MM'. ({e})"
                )
            if target <= datetime.now():
                raise ValueError(
                    f"One-time schedule '{raw}' is in the past — it would "
                    "never fire. Pick a future time."
                )
        else:
            try:
                cron_matches(schedule)
            except ValueError as e:
                raise ValueError(f"Invalid schedule: {e}")
            # Reject crons that match no calendar date within the next year
            # (e.g. '0 0 30 2 *' — Feb 30 never exists).
            if next_cron_run(schedule) is None:
                raise ValueError(
                    f"Cron expression '{schedule}' does not match any date in "
                    "the next year — check the day-of-month / month fields."
                )

        task_id = str(uuid.uuid4())[:12]
        now = datetime.now().isoformat()
        # Predicate condition paradigm (shared with timer_watchers): a proactive
        # agent (task_type='agent') can carry a shell PREDICATE that reconciles
        # against the poll LLM and auto-promotes to zero-cost `code`. The kind is
        # derived from the parameter combination, NEVER exposed as an LLM knob.
        from lib.scheduler._shared import derive_condition_kind

        condition_kind = (
            derive_condition_kind(command, condition_command)
            if task_type == "agent"
            else "llm"
        )

        payload = {
            "task_id": task_id,
            "user_id": user_id,
            "name": name,
            "schedule": schedule,
            "task_type": task_type,
            "command": command,
            "description": description or "",
            "enabled": True,
            "notify_on_failure": bool(notify_on_failure),
            "notify_on_success": bool(notify_on_success),
            "max_runtime": int(max_runtime),
            "created_at": now,
            "updated_at": now,
            "target_conv_id": target_conv_id or "",
            "source_conv_id": source_conv_id or "",
            "tools_config": tools_config or {},
            "max_executions": int(max_executions),
            "expires_at": expires_at or "",
            "condition_kind": condition_kind,
            "condition_command": condition_command or "",
            "condition_regex": condition_regex or "",
        }
        result = _scheduler_client(write=True).command(
            "scheduler.task.create", payload, task_id
        )
        task = _scheduler_wire_task(result["task"])

        audit_log(
            'scheduled_task_created',
            task_id=task_id,
            task_type=task_type,
            principal_kind=principal.kind,
            principal_subject_id=principal.subject_id,
            owner_user_id=user_id,
            tenant_id=principal.tenant_id,
        )

        logger.info(
            '✅ Created task "%s" (id=%s, type=%s, schedule=%s, target_conv=%s)',
            name,
            task_id,
            task_type,
            schedule,
            target_conv_id or "N/A",
        )
        return task

    def list_tasks(self, *, user_id: int, include_disabled=False):
        """List all tasks."""
        rows = _scheduler_client().query(
            "scheduler.task.list",
            {
                "user_id": int(user_id),
                "limit": 1000,
                "enabled_only": not include_disabled,
            },
        )
        rows = [_scheduler_wire_task(row) for row in rows]

        tasks = []
        for r in rows:
            t = dict(r)
            # Add next run time
            if not t["schedule"].startswith("once:") and t["enabled"]:
                try:
                    nxt = next_cron_run(t["schedule"])
                    t["next_run"] = nxt.isoformat() if nxt else None
                except Exception as e:
                    logger.debug(
                        "[Scheduler] next_cron_run parse failed for task %s schedule=%s: %s",
                        t.get("id", "?"),
                        t.get("schedule", "?"),
                        e,
                        exc_info=True,
                    )
                    t["next_run"] = None
            else:
                t["next_run"] = None
            t["schedule_human"] = (
                describe_cron(t["schedule"])
                if not t["schedule"].startswith("once:")
                else f"once at {t['schedule'][5:]}"
            )
            tasks.append(t)

        return tasks

    def get_task(self, task_id, *, user_id: int):
        """Get a single task by ID."""
        row = _scheduler_client().query(
            "scheduler.task.get", {"task_id": task_id, "user_id": int(user_id)}
        )
        return _scheduler_wire_task(row) if row else None

    def update_task(self, task_id, *, user_id: int, **kwargs):
        """Update task fields."""
        allowed = {
            "name",
            "schedule",
            "command",
            "task_type",
            "description",
            "enabled",
            "notify_on_failure",
            "notify_on_success",
            "max_runtime",
            "target_conv_id",
            "source_conv_id",
            "tools_config",
            "poll_count",
            "last_poll_at",
            "last_poll_decision",
            "last_poll_reason",
            "last_execution_at",
            "last_execution_task_id",
            "last_execution_status",
            "execution_count",
            "max_executions",
            "expires_at",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return False

        updates["updated_at"] = datetime.now().isoformat()

        if "tools_config" in updates and isinstance(updates["tools_config"], str):
            try:
                updates["tools_config"] = json.loads(updates["tools_config"])
            except (TypeError, ValueError):
                pass
        updates["task_id"] = task_id
        updates["user_id"] = int(user_id)
        return bool(
            _scheduler_client(write=True)
            .command(
                "scheduler.task.update",
                updates,
                f"scheduler.update:{task_id}:{uuid.uuid4().hex}",
            )
            .get("changed")
        )

    def delete_task(self, task_id, *, user_id: int):
        """Delete a task."""
        changed = _scheduler_client(write=True).command(
            "scheduler.task.delete",
            {"task_id": task_id, "user_id": int(user_id)},
            f"scheduler.delete:{task_id}:{uuid.uuid4().hex}",
        )
        logger.info("🗑️ Deleted task %s", task_id)
        return bool(changed.get("deleted"))

    def toggle_task(self, task_id, *, user_id: int, enabled=None):
        """Enable or disable a task."""
        current = self.get_task(task_id, user_id=user_id)
        if current is None:
            return None
        value = (
            not bool(current.get("enabled")) if enabled is None else bool(enabled)
        )
        changed = self.update_task(task_id, user_id=user_id, enabled=int(value))
        return value if changed else None

    def get_execution_log(self, *, user_id: int, limit=20):
        """Get recent in-process executions visible to one owner."""
        with self._log_lock:
            visible = [
                entry
                for entry in self._execution_log
                if int(entry.get("user_id") or 0) == int(user_id)
            ]
            return list(visible[-limit:])

    def get_task_history(
        self,
        task_id: str,
        *,
        user_id: int,
        limit: int = 20,
    ) -> list[dict]:
        """Return recent in-process executions for an owner-scoped task."""
        if self.get_task(task_id, user_id=user_id) is None:
            return []
        entries = self.get_execution_log(user_id=user_id, limit=max(1, int(limit)) * 5)
        return [entry for entry in entries if entry.get("task_id") == task_id][
            -max(1, int(limit)) :
        ]

    # ── Task Execution ──

    def _execute_task(self, task):
        """Execute a single task. Returns (success, result_text)."""
        task_type = task["task_type"]
        command = task["command"]
        max_runtime = task.get("max_runtime", 300)

        logger.info(
            "[Scheduler] Executing task type=%s cmd=%s", task_type, str(command)[:100]
        )

        # Distributed PostgreSQL backup/PITR belongs to the hosting platform.
        if (task_type == 'storage_backup'
                and not _application_managed_storage_backups_enabled()):
            logger.warning(
                '[Scheduler] Blocked application-managed database maintenance '
                'task type=%s id=%s on this deployment',
                task_type,
                task.get('id', '?'),
            )
            return (
                False,
                'Blocked: application-managed database backups are unavailable '
                'on this deployment.',
            )

        # Defense-in-depth: re-check the code-exec gate at execution time so a
        # task created while the flag was ON cannot keep running arbitrary code
        # after an operator flips SCHEDULER_ALLOW_CODE_EXEC off.
        if task_type in _CODE_EXEC_TASK_TYPES:
            import lib as _lib

            if not getattr(_lib, "SCHEDULER_ALLOW_CODE_EXEC", True):
                logger.warning(
                    '[Scheduler] Blocked %s task "%s" — '
                    "SCHEDULER_ALLOW_CODE_EXEC is off",
                    task_type,
                    task.get("name", "?"),
                )
                return False, "Blocked: code execution disabled on this deployment."
            audit_log(
                "scheduled_code_exec",
                task_id=task.get("id", "?"),
                task_name=task.get("name", "?"),
                task_type=task_type,
                command=str(command)[:500],
            )

        if task_type == "command":
            try:
                from lib.compat import get_shell_args

                execution = run_bounded_process(
                    get_shell_args(command),
                    max_runtime=max_runtime,
                    job_id=str(task.get('id') or ''),
                    job_type=task_type,
                    principal=_task_process_principal(task),
                    cancel_event=getattr(self, '_stop_event', None),
                )
                if execution.error:
                    return False, execution.error
                output = execution.stdout
                if execution.stderr:
                    output += f"\n[stderr] {execution.stderr}"
                success = execution.returncode == 0
                return (
                    success,
                    output if output.strip() else f"(exit code: {execution.returncode})",
                )
            except Exception as e:
                logger.error(
                    "[Scheduler] Command task failed: cmd=%s: %s",
                    str(command)[:100],
                    e,
                    exc_info=True,
                )
                return False, "Command execution error (see logs)"

        elif task_type == "python":
            try:
                execution = run_bounded_process(
                    [sys.executable, "-c", command],
                    max_runtime=max_runtime,
                    job_id=str(task.get('id') or ''),
                    job_type=task_type,
                    principal=_task_process_principal(task),
                    cancel_event=getattr(self, '_stop_event', None),
                )
                if execution.error:
                    return False, execution.error
                output = execution.stdout
                if execution.stderr:
                    output += f"\n[stderr] {execution.stderr}"
                return (
                    execution.returncode == 0,
                    output or f"(exit code: {execution.returncode})",
                )
            except Exception as e:
                logger.error(
                    "[Scheduler] Python task failed: cmd=%s: %s",
                    str(command)[:100],
                    e,
                    exc_info=True,
                )
                return False, "Python execution error (see logs)"

        elif task_type == "prompt":
            # Use LLM to answer a prompt — useful for periodic analysis
            try:
                from lib.llm_dispatch import smart_chat

                content, usage = smart_chat(
                    messages=[{"role": "user", "content": command}],
                    max_tokens=4096,
                    log_prefix="[Scheduler]",
                )
                return True, content
            except Exception as e:
                logger.error(
                    "[Scheduler] Prompt task failed: cmd=%s: %s",
                    str(command)[:100],
                    e,
                    exc_info=True,
                )
                return False, "Prompt execution error (see logs)"

        elif task_type == "storage_backup":
            # The Sidecar is the only component that can consistently snapshot
            # its live authority. This scheduler thread merely invokes that
            # bounded semantic maintenance operation.
            try:
                summary = _scheduler_client().maintenance(
                    'system.backup', deadline=float(max_runtime),
                )
                if not isinstance(summary, dict) or not summary.get('ok'):
                    raise RuntimeError('storage backup returned an invalid result')
                size_mib = int(summary.get('bytes') or 0) / (1024 * 1024)
                return True, (
                    f"backup ok: {summary.get('backup')} "
                    f"({size_mib:.1f} MiB; manifest {summary.get('manifest')})"
                )
            except Exception as e:
                logger.error(
                    "[Scheduler] storage backup task failed: %s", e, exc_info=True
                )
                return False, "Storage backup error (see logs)"

        elif task_type == 'daily_report_backfill':
            # Daily reports are owner-scoped durable state. The command is
            # informational; execution rebuilds only reports:maintain for the
            # owner recorded on this exact Sidecar task row.
            return _run_daily_report_backfill(task)

        elif task_type == "reserve_reclaim":
            # Billing janitor: release reservations orphaned by a crash/abort
            # before settle (lib.billing.wallet_janitor.sweep_stale_reserves).
            # ``command`` is informational only. No-op when billing is inactive
            # (the sweep simply finds nothing to reclaim).
            try:
                from lib.billing.wallet_janitor import sweep_stale_reserves

                summary = sweep_stale_reserves()
                return True, (
                    f"reclaimed {summary.get('reclaimed', 0)}/"
                    f"{summary.get('candidates', 0)} hold(s), "
                    f"{summary.get('reclaimed_micro', 0)}µ "
                    f"(errors={summary.get('errors', 0)})"
                )
            except Exception as e:
                logger.error(
                    "[Scheduler] reserve_reclaim task failed: %s", e, exc_info=True
                )
                return False, "Reserve reclaim error (see logs)"

        elif task_type == "optimizer":
            # Daily Optimizer: runs lib.optimizer.run_once() in-process.
            # ``command`` is informational only (the handler ignores it so
            # the LLM cannot inject arbitrary code).
            try:
                import lib as _lib

                if not getattr(_lib, "OPTIMIZER_ENABLED", True):
                    logger.info(
                        "[Scheduler] Optimizer task skipped — OPTIMIZER_ENABLED=False"
                    )
                    return True, "skipped (optimizer disabled in Settings)"
                from lib.optimizer import run_once
                import json as _json

                summary = run_once(
                    principal=_task_process_principal(
                        task, system_scope='optimizer:maintain'),
                    dry_run=False,
                )
                text = _json.dumps(
                    {
                        "proposals": len(summary.get("proposals", [])),
                        "applied": len(summary.get("applied", [])),
                        "pending_review": len(summary.get("pending_review", [])),
                        "rejected": len(summary.get("rejected", [])),
                        "reverts": len(summary.get("reverts", [])),
                    },
                    ensure_ascii=False,
                )
                return True, text
            except Exception as e:
                logger.error("[Scheduler] Optimizer task failed: %s", e, exc_info=True)
                return False, "Optimizer execution error (see logs)"

        return False, f"Unknown task type: {task_type}"

    def run_task_now(self, task_id, *, user_id: int):
        """Manually trigger a task immediately."""
        task = self.get_task(task_id, user_id=user_id)
        if not task:
            return None, "Task not found"

        logger.info('▶️ Running task "%s" (manual trigger)', task["name"])
        success, result = self._execute_task(task)

        now = datetime.now().isoformat()
        _scheduler_client(write=True).command(
            "scheduler.task.record_result",
            {
                "task_id": task_id,
                "user_id": int(user_id),
                "now": now,
                "result": result,
                "success": bool(success),
            },
            f"scheduler.result:{task_id}:{now}",
        )

        status = "✅" if success else "❌"
        logger.info('%s Task "%s" → %s', status, task["name"], result[:200])

        with self._log_lock:
            self._execution_log.append(
                {
                    "task_id": task_id,
                    "user_id": int(user_id),
                    "task_name": task["name"],
                    "time": now,
                    "success": success,
                    "result": result[:2000],
                }
            )
            # Keep log bounded
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]

        return success, result

    # ── Background Scheduler ──

    def _queue_startup_task(self, task_id: str) -> None:
        """Queue one bounded startup hint; the durable claim stays authority."""
        maintenance_lock = getattr(self, '_maintenance_lock', None)
        if maintenance_lock is None:
            # Compatibility for narrow embedders/tests that historically
            # constructed the manager with __new__ and supplied only the
            # execution seams they exercised.
            maintenance_lock = self._maintenance_lock = threading.Lock()
        with maintenance_lock:
            if not hasattr(self, '_startup_task_ids'):
                self._startup_task_ids = set()
            self._startup_task_ids.add(str(task_id))

    def _take_startup_tasks(self) -> set[str]:
        """Snapshot hints without dropping one absent from an in-flight query."""
        maintenance_lock = getattr(self, '_maintenance_lock', None)
        if maintenance_lock is None:
            return set()
        with maintenance_lock:
            startup_task_ids = getattr(self, '_startup_task_ids', set())
            return set(startup_task_ids)

    def _complete_startup_task(self, task_id: str) -> None:
        maintenance_lock = getattr(self, '_maintenance_lock', None)
        if maintenance_lock is None:
            return
        with maintenance_lock:
            getattr(self, '_startup_task_ids', set()).discard(str(task_id))

    def _dispatch_claimed_task(self, task: dict) -> None:
        if task['task_type'] == 'agent':
            self._run_proactive_poll(task)
        elif task['task_type'] in _ASYNC_TASK_TYPES:
            self._dispatch_maintenance_task(task)
        else:
            self._run_and_record(task)

    def _check_and_run_due_tasks(self):
        """Check all tasks and run any that are due."""
        process_principal = self._background_principal()
        now = datetime.now()
        tasks = _scheduler_client().query(
            "scheduler.task.list_all", {"limit": 1000, "enabled_only": True}
        )
        tasks = [_scheduler_wire_task(task) for task in tasks]
        startup_task_ids = self._take_startup_tasks()

        for task in tasks:
            task = dict(task)
            if str(task['id']) in startup_task_ids:
                claimed = self._claim_due_task(task, now)
                self._complete_startup_task(str(task['id']))
                if claimed:
                    self._dispatch_claimed_task(task)
                continue
            schedule = task["schedule"]

            # One-time tasks
            if schedule.startswith("once:"):
                try:
                    target_time = datetime.fromisoformat(schedule[5:].strip())
                except ValueError:
                    logger.warning(
                        "[Scheduler] invalid once: schedule for task %s: %s — skipping",
                        task.get("id", "?"),
                        schedule,
                        exc_info=True,
                    )
                    continue
                if now >= target_time:
                    # Check if already run
                    if task["run_count"] > 0:
                        continue
                    if not self._claim_due_task(task, now):
                        continue
                    self._dispatch_claimed_task(task)
                    # Auto-disable after one-time run
                    self.toggle_task(
                        task["id"], user_id=int(task["user_id"]), enabled=False
                    )
                continue

            # Cron tasks
            try:
                if not cron_matches(schedule, now):
                    continue
            except ValueError:
                logger.debug(
                    "[Scheduler] invalid cron expression for task %s: %s",
                    task.get("id", "?"),
                    schedule,
                    exc_info=True,
                )
                continue
            if not self._claim_due_task(task, now):
                continue

            self._dispatch_claimed_task(task)

        # ── Project Brain heartbeat (Pillar #5 sweep) ──
        #   After the due-task pass, dispatch any genuinely-pickable board epics
        #   on idle projects — this is what STARTS work when nothing just
        #   completed and no human is typing (incl. the cold-start first epic).
        #   Reuses THIS existing 30s tick (no new thread/global); idempotent via
        #   claim-on-dispatch + busy-guard; best-effort so a sweep failure can
        #   never break the scheduler loop.
        personal_owner_user_id = process_principal.owner_user_id
        if personal_owner_user_id is not None:
            try:
                from lib.conversations.project_dispatch import (
                    sweep_all_active_projects,
                )

                sweep_all_active_projects(user_id=personal_owner_user_id)
            except Exception as e:
                logger.warning(
                    "[Scheduler] project-brain dispatch sweep skipped: %s", e)

        # ── Peer-message idle-drain (Pillar #6 Symptom-A fix) ──
        #   The workflow sweep above only reconciles KIND_WORKFLOW kickoffs. A
        #   KIND_PEER_MSG row that landed in an IDLE, non-board conversation is
        #   drained by nothing in steady state — it would sit in the queue
        #   widget forever, shown but never rendered as a turn. This drains one
        #   such row per idle conv via the same dispatch_next_queued seam, so an
        #   advisory peer note to an idle sibling wakes a fresh turn (rendered
        #   with the .peer-msg-banner). Global scan (the queue has no project
        #   column; the per-(sender,target) send-time rate cap already bounds
        #   how many peer rows can exist). Best-effort.
        # TODO(enterprise): authorize the global scan with a queue:dispatch
        # system principal. Until then an ownerless distributed scheduler must
        # fail closed instead of acquiring ambient cross-owner authority.
        if personal_owner_user_id is not None:
            try:
                from lib.message_queue import drain_idle_peer_messages

                drain_idle_peer_messages()
            except Exception as e:
                logger.warning(
                    "[Scheduler] peer-message idle-drain skipped: %s", e)

    def _run_and_record(self, task):
        """Run task and record result in DB."""
        task_id = task["id"]
        logger.info('▶️ Running scheduled task "%s"', task["name"])

        success, result = self._execute_task(task)

        now = datetime.now().isoformat()
        _scheduler_client(write=True).command(
            "scheduler.task.record_result",
            {
                "task_id": task_id,
                "user_id": int(task["user_id"]),
                "now": now,
                "result": result,
                "success": bool(success),
            },
            f"scheduler.result:{task_id}:{now}",
        )

        status = "✅" if success else "❌"
        logger.info('%s "%s" → %s', status, task["name"], result[:200])

        with self._log_lock:
            self._execution_log.append(
                {
                    "task_id": task_id,
                    "user_id": int(task["user_id"]),
                    "task_name": task["name"],
                    "time": now,
                    "success": success,
                    "result": result[:2000],
                }
            )
            if len(self._execution_log) > 100:
                self._execution_log = self._execution_log[-50:]

    def _dispatch_maintenance_task(self, task: dict) -> bool:
        """Run a claimed maintenance job without blocking the 30-second tick."""
        task_id = str(task['id'])
        with self._maintenance_lock:
            existing = self._maintenance_threads.get(task_id)
            if existing is not None and existing.is_alive():
                logger.warning(
                    '[Scheduler] maintenance task %s is already running', task_id)
                return False

            def run_and_release() -> None:
                try:
                    self._run_and_record(dict(task))
                except Exception as exc:
                    logger.error(
                        '[Scheduler] maintenance task %s failed outside result '
                        'recording: %s', task_id, exc, exc_info=True)
                finally:
                    with self._maintenance_lock:
                        current = self._maintenance_threads.get(task_id)
                        if current is threading.current_thread():
                            self._maintenance_threads.pop(task_id, None)

            thread = threading.Thread(
                target=run_and_release,
                name=f'tofu-maintenance-{task_id[:24]}',
                daemon=True,
            )
            self._maintenance_threads[task_id] = thread
            thread.start()
            return True

    def _run_proactive_poll(self, task):
        """Run the proactive agent poll→decide→execute cycle for a task_type='agent'.

        Phase B: Lightweight LLM poll (cheap model, no tools, independent context).
        Phase C: If poll says act=true, create full agentic task in target conversation.
        """
        from lib.scheduler.proactive import (
            apply_reconcile_poll,
            evaluate_condition_predicate,
            execute_proactive_task,
            gather_system_status,
            is_task_executing,
            poll_decision,
            record_poll,
            should_auto_disable,
        )

        task_id = task["id"]
        pfx = f"[Proactive:{task_id[:8]}]"

        # ── Pre-checks ──
        if should_auto_disable(task):
            self.update_task(task_id, user_id=int(task["user_id"]), enabled=False)
            logger.info("%s Auto-disabled (max_executions or expired)", pfx)
            return

        if is_task_executing(task):
            logger.debug(
                "%s Skipping poll — previous execution still running (task_id=%s)",
                pfx,
                task.get("last_execution_task_id", "?")[:8],
            )
            return

        # ── Phase B: Poll (tiered — predicate / LLM / hybrid) ──
        kind = task.get("condition_kind", "llm")
        logger.info(
            "%s Starting poll #%d (kind=%s)", pfx, task.get("poll_count", 0) + 1, kind
        )
        status_snapshot = gather_system_status(task)

        # tier / predicate_matched / llm_agreed audit trio for the ledger.
        tier, predicate_matched, llm_agreed = "llm", -1, -1
        tokens_used = 0

        if kind == "code":
            # Pure predicate — ZERO LLM. Ambiguous/errored predicate → NOT act
            # (never a false-positive trigger); a sustained ambiguity run demotes
            # back to hybrid so the LLM re-takes the wheel (self-healing).
            pred = evaluate_condition_predicate(task)
            outcome = apply_reconcile_poll(
                task, pred, llm_ready=None, llm_available=False
            )
            should_act = outcome.authoritative_ready
            reason = outcome.note
            tier, predicate_matched, llm_agreed = (
                outcome.tier,
                outcome.predicate_matched,
                outcome.llm_agreed,
            )
        else:
            should_act, reason, tokens_used = poll_decision(task)
            if kind == "hybrid":
                # LLM authoritative; reconcile the predicate alongside so the
                # condition can auto-promote to `code` after enough agreements.
                # poll_decision returns should_act=False on a parse/LLM error;
                # treat that as an unusable verdict → llm_available=False → the
                # reconcile resets the streak and never promotes on a bad poll.
                _llm_ok = not str(reason).startswith(("Parse error", "LLM error"))
                pred = evaluate_condition_predicate(task)
                outcome = apply_reconcile_poll(
                    task, pred, llm_ready=should_act, llm_available=_llm_ok
                )
                should_act = outcome.authoritative_ready
                reason = f"{reason} [{outcome.note}]"
                tier, predicate_matched, llm_agreed = (
                    outcome.tier,
                    outcome.predicate_matched,
                    outcome.llm_agreed,
                )

        decision = "act" if should_act else "skip"
        now = datetime.now().isoformat()

        # Update task poll state in DB
        current = self.get_task(task_id, user_id=int(task["user_id"])) or {}
        self.update_task(
            task_id,
            user_id=int(task["user_id"]),
            poll_count=int(current.get("poll_count", 0) or 0) + 1,
            last_poll_at=now,
            last_poll_decision=decision,
            last_poll_reason=reason[:500],
            last_run=now,
        )

        logger.info(
            "%s Poll decision: %s — reason: %s (tokens=%d, tier=%s)",
            pfx,
            decision,
            reason[:100],
            tokens_used,
            tier,
        )

        if not should_act:
            record_poll(
                task_id,
                "skip",
                reason,
                "cheap",
                tokens_used,
                status_snapshot,
                tier=tier,
                predicate_matched=predicate_matched,
                llm_agreed=llm_agreed,
                user_id=int(task["user_id"]),
            )
            return

        # ── Phase C: Execute ──
        exec_task_id = execute_proactive_task(task)

        if exec_task_id:
            # Update execution state
            current = self.get_task(task_id, user_id=int(task["user_id"])) or {}
            self.update_task(
                task_id,
                user_id=int(task["user_id"]),
                last_execution_at=now,
                last_execution_task_id=exec_task_id,
                last_execution_status="running",
                execution_count=int(current.get("execution_count", 0) or 0) + 1,
            )

            record_poll(
                task_id,
                "act",
                reason,
                "cheap",
                tokens_used,
                status_snapshot,
                execution_task_id=exec_task_id,
                tier=tier,
                predicate_matched=predicate_matched,
                llm_agreed=llm_agreed,
                user_id=int(task["user_id"]),
            )
            logger.info(
                "%s 🚀 Execution started: agentic_task=%s", pfx, exec_task_id[:8]
            )
        else:
            record_poll(
                task_id,
                "act_failed",
                reason,
                "cheap",
                tokens_used,
                status_snapshot,
                tier=tier,
                predicate_matched=predicate_matched,
                llm_agreed=llm_agreed,
                user_id=int(task["user_id"]),
            )
            logger.error("%s ❌ Execution failed to start", pfx)
            audit_log(
                "proactive_exec_failed",
                task_id=task_id,
                task_name=task.get("name", "?"),
                reason=str(reason)[:200],
            )

    def _ensure_default_optimizer_task(self):
        """Idempotently register the Daily Optimizer cron task.

        Runs ``lib.optimizer.run_once()`` nightly at 03:30 local.  Matched
        by exact name so subsequent boots never create duplicates.
        """
        try:
            task, created = self._ensure_default_task(
                system_key="daily-optimizer",
                name="Daily Optimizer",
                schedule="30 3 * * *",
                command="lib.optimizer.run_once()",
                task_type="optimizer",
                description="Mines logs + daily reports once per day and applies "
                "whitelisted optimisations (block_search_domain). "
                "Auto-registered by lib.scheduler.manager.",
                max_runtime=600,
            )
            import lib as _lib

            if created and not getattr(_lib, "OPTIMIZER_ENABLED", True):
                self.toggle_task(
                    task["id"], user_id=_task_owner_user_id(task), enabled=False
                )
            if created:
                logger.info(
                    "[Scheduler] Auto-registered Daily Optimizer task id=%s",
                    task.get("id"),
                )
        except Exception as e:
            logger.debug(
                "[Scheduler] Could not auto-register Daily Optimizer "
                "(will retry on next startup): %s",
                e,
            )

    def _ensure_default_storage_backup_task(self):
        """Idempotently register the personal Sidecar backup task."""
        try:
            task, created = self._ensure_default_task(
                system_key="storage-backup",
                name="Storage Backup",
                schedule="0 2 * * *",
                command="storage.system.backup",
                task_type="storage_backup",
                description="Nightly verified Sidecar snapshot with a durable "
                "checksum manifest under data/backups/. "
                "Auto-registered by lib.scheduler.manager.",
                max_runtime=1800,
            )
            if created:
                logger.info(
                    "[Scheduler] Auto-registered Storage Backup task id=%s",
                    task.get("id"),
                )
        except Exception as e:
            logger.debug(
                "[Scheduler] Could not auto-register Storage Backup "
                "(will retry after schema ready): %s",
                e,
            )

    def _ensure_default_daily_report_task(self):
        """Install the durable six-hour report backfill for this owner."""
        try:
            task, created = self._ensure_default_task(
                system_key='daily-report-backfill',
                name='Daily Report Backfill',
                schedule='0 */6 * * *',
                command='lib.daily_report._backfill_yesterday_if_missing()',
                task_type='daily_report_backfill',
                description='Generates yesterday\'s owner-scoped report only '
                'when it is missing. Auto-registered by '
                'lib.scheduler.manager.',
                max_runtime=900,
                enabled=True,
                reconcile_enabled=True,
            )
            owner_user_id = _task_owner_user_id(task)
            from lib.daily_report.storage import _load_report

            yesterday = (datetime.now().date() - timedelta(days=1)).isoformat()
            if _load_report(yesterday, owner_user_id=owner_user_id) is None:
                self._queue_startup_task(str(task['id']))
            if created:
                logger.info(
                    '[Scheduler] Auto-registered Daily Report Backfill '
                    'task id=%s', task.get('id'))
            return task
        except Exception as e:
            logger.debug(
                '[Scheduler] Could not auto-register Daily Report Backfill '
                '(will retry after schema ready): %s', e)
            return None

    def _ensure_default_reserve_reclaim_task(self):
        """Idempotently register the billing reserve-reclaim cron task.

        Runs ``lib.billing.wallet_janitor.sweep_stale_reserves()`` every 5
        minutes. This is the money-correctness safety net: a request that
        crashes between ``reserve(-estimate)`` and ``settle`` would otherwise
        leave the hold subtracted from the user's usable balance forever. The
        sweep releases such orphans (older than TOFU_BILLING_RESERVE_TTL,
        default 30 min) via the idempotent ``reserve_release`` path. Matched
        by exact name so subsequent boots never create duplicates. The durable
        row is disabled unless multi-user billing is actually active.
        """
        try:
            from lib.billing.wallet_janitor import reserve_reclaim_enabled

            enabled = reserve_reclaim_enabled()
            task, created = self._ensure_default_task(
                system_key="billing-reserve-reclaim",
                name="Billing Reserve Reclaim",
                schedule="*/5 * * * *",
                command="lib.billing.wallet_janitor.sweep_stale_reserves()",
                task_type="reserve_reclaim",
                description="Releases billing reservations orphaned by a crash/"
                "abort before settle (older than "
                "TOFU_BILLING_RESERVE_TTL, default 30 min). "
                "Money-correctness safety net. Auto-registered by "
                "lib.scheduler.manager.",
                max_runtime=300,
                enabled=enabled,
                reconcile_enabled=True,
            )
            if created:
                logger.info(
                    "[Scheduler] Auto-registered Billing Reserve Reclaim "
                    "task id=%s enabled=%s", task.get("id"), enabled,
                )
        except Exception as e:
            logger.debug(
                "[Scheduler] Could not auto-register Billing Reserve "
                "Reclaim (will retry after schema ready): %s",
                e,
            )

    def start(self, *, principal: PrincipalContext) -> bool:
        """Start the background scheduler thread."""
        principal = _validated_scheduler_process_principal(principal)
        if self._thread is not None and self._thread.is_alive():
            if self._process_principal != principal:
                raise RuntimeError(
                    'running scheduler principal cannot be replaced')
            return False
        self._process_principal = principal
        self._running = True
        self._stop_event.clear()

        def _loop():
            logger.info("🕐 Background scheduler started")
            while self._running:
                try:
                    self._check_and_run_due_tasks()
                except Exception as e:
                    # Transient DB connection errors (PG timeout, connection
                    # reset) are routinely recoverable on the next 30s tick —
                    # downgrade to WARNING without a traceback so they don't
                    # pollute error.log. Anything else is still a real bug.
                    from lib.storage import StorageError

                    etype = type(e).__name__
                    is_transient_db = isinstance(e, StorageError) and e.retryable
                    if is_transient_db:
                        logger.warning(
                            "[Scheduler] Transient DB error in check loop "
                            "(will retry in 30s): %s: %s",
                            etype,
                            e,
                        )
                    else:
                        logger.error(
                            "[Scheduler] Error in scheduler check loop: %s",
                            e,
                            exc_info=True,
                        )
                if self._stop_event.wait(30):
                    break

        self._thread = threading.Thread(target=_loop, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 2.0) -> bool:
        """Stop the tick and cancel/drain maintenance children by deadline."""
        self._running = False
        self._stop_event.set()
        thread = self._thread
        try:
            wait_seconds = max(0.0, float(timeout))
        except (TypeError, ValueError, OverflowError) as exc:
            logger.debug("[Scheduler] invalid stop timeout; using 2.0: %s", exc)
            wait_seconds = 2.0
        deadline = time.monotonic() + wait_seconds
        if (thread is not None
                and thread is not threading.current_thread()):
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        scheduler_stopped = thread is None or not thread.is_alive()
        if scheduler_stopped and self._thread is thread:
            self._thread = None
        maintenance_lock = getattr(self, '_maintenance_lock', None)
        if maintenance_lock is None:
            maintenance_threads = []
        else:
            with maintenance_lock:
                maintenance_threads = list(
                    getattr(self, '_maintenance_threads', {}).values())
        for maintenance_thread in maintenance_threads:
            if maintenance_thread is threading.current_thread():
                continue
            maintenance_thread.join(
                timeout=max(0.0, deadline - time.monotonic()))
        maintenance_stopped = all(
            not item.is_alive() for item in maintenance_threads)
        if not scheduler_stopped or not maintenance_stopped:
            return False
        logger.info("Stopped")
        return True


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


def start_scheduler_worker(*, principal: PrincipalContext):
    """Start the background scheduler thread and resume active timers.

    Called from ``register_all`` in ``routes/__init__.py`` after the storage
    Sidecar is ready. Built-in task reconciliation is atomic and active timers
    are resumed before this function returns.

    Set ``TOFU_DISABLE_SCHEDULER=1`` to skip starting the worker entirely
    — the test suite sets this so importing ``server`` (which many tests do)
    does NOT spin up a real 30s-tick scheduler + timer-resume thread that
    would run live LLM polls / web searches against the shared DB, stealing
    CPU/IO and making timing-sensitive tests flaky.
    """
    import os as _os

    principal = _validated_scheduler_process_principal(principal)

    if _os.environ.get("TOFU_DISABLE_SCHEDULER", "").lower() in ("1", "true", "yes"):
        logger.info(
            "[Scheduler] Background worker disabled (TOFU_DISABLE_SCHEDULER set)"
        )
        return get_scheduler()

    mgr = get_scheduler()
    mgr.start(principal=principal)
    logger.info("[Scheduler] Background scheduler worker started")

    bootstraps = []
    if principal.owner_user_id is not None:
        bootstraps.append(mgr._ensure_default_daily_report_task)
        bootstraps.append(mgr._ensure_default_optimizer_task)
        if _application_managed_storage_backups_enabled():
            bootstraps.append(mgr._ensure_default_storage_backup_task)
        else:
            logger.info(
                '[Scheduler] Skipping application-managed database backup '
                'tasks; the deployment platform owns PostgreSQL backup/PITR')
        bootstraps.append(mgr._ensure_default_reserve_reclaim_task)
    else:
        logger.info(
            '[Scheduler] Distributed scheduler has no implicit owner; personal '
            'built-in tasks and project/peer sweeps are disabled')

    for bootstrap in bootstraps:
        try:
            bootstrap()
        except Exception as e:
            logger.warning("[Scheduler] built-in task bootstrap failed: %s", e)

    try:
        from lib.scheduler.timer import resume_active_timers

        resumed = resume_active_timers()
        if resumed:
            logger.info("[Scheduler] Resumed %d active timer(s)", resumed)
    except Exception as e:
        logger.warning("[Scheduler] Failed to resume timers on startup: %s", e)
    return mgr


def stop_scheduler_worker(timeout: float = 2.0) -> bool:
    """Stop the process scheduler without constructing it during shutdown."""
    with _manager_lock:
        manager = _manager
    if manager is None:
        return True
    return manager.stop(timeout=timeout)


def ensure_daily_report_schedule(*, principal: PrincipalContext) -> bool:
    """Reconcile the report built-in through an already-owned scheduler."""
    if not isinstance(principal, PrincipalContext):
        raise TypeError('daily report schedule requires PrincipalContext')
    owner_user_id = principal.require_owner(context='daily report schedule')
    principal.require_scope('reports:maintain')
    with _manager_lock:
        manager = _manager
    if manager is None or not manager._running:
        logger.debug(
            '[Scheduler] daily report compatibility start deferred: '
            'scheduler worker is not running')
        return False
    scheduler_owner = manager._background_principal().require_owner(
        context='daily report schedule')
    if scheduler_owner != owner_user_id:
        raise PermissionError('daily report owner does not match scheduler owner')
    return manager._ensure_default_daily_report_task() is not None


__all__ = [
    "ScheduledTaskManager",
    "get_scheduler",
    "ensure_daily_report_schedule",
    "start_scheduler_worker",
    "stop_scheduler_worker",
]
