"""lib/scheduler/proactive.py — Proactive Agent: poll → decide → execute.

The proactive agent extends the scheduler with a new task_type='agent'
that runs a two-phase cycle:

  Phase B (Poll):  Lightweight LLM call with a status snapshot.
                   The LLM decides: act now, or skip.
                   Each poll is INDEPENDENT (no history of prior polls).

  Phase C (Execute): Full agentic task in the target conversation
                     with ALL tools, SSE streaming, visible to the
                     frontend like any user-initiated task.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from lib.log import get_logger, log_context

logger = get_logger(__name__)

# ── Status snapshot builder ─────────────────────────────────────────────────

def gather_system_status(task: dict[str, Any]) -> str:
    """Build a compact status report for the poll LLM.

    Includes:
    - Active tasks (running conversations)
    - Target conversation summary (last messages)
    - System time
    """
    from lib.tasks_pkg.manager import tasks, tasks_lock

    lines = ['=== Proactive Task Status Report ===']
    lines.append(f'Task: "{task["name"]}"')
    lines.append(f'Poll #{task.get("poll_count", 0) + 1}')

    last_poll = task.get('last_poll_at') or 'never'
    last_decision = task.get('last_poll_decision') or 'none'
    lines.append(f'Last poll: {last_poll} (decision: {last_decision})')

    # Active tasks
    with tasks_lock:
        running = [
            {
                'task_id': t['id'][:12],
                'conv_id': t.get('convId', '?')[:12],
                'status': t['status'],
                'elapsed': round(time.time() - t.get('created_at', time.time())),
            }
            for t in tasks.values()
            if t.get('status') == 'running'
        ]

    if running:
        lines.append(f'\nActive tasks ({len(running)} running):')
        for r in running:
            lines.append(f'  🔄 task={r["task_id"]} conv={r["conv_id"]} '
                         f'running for {r["elapsed"]}s')
    else:
        lines.append('\nNo tasks currently running. All conversations are idle.')

    # Target conversation summary
    target_conv = task.get('target_conv_id', '')
    if target_conv:
        try:
            from lib.database import DOMAIN_CHAT, get_thread_db
            db = get_thread_db(DOMAIN_CHAT)
            row = db.execute(
                'SELECT title, messages, msg_count FROM conversations WHERE id=? AND user_id=1',
                (target_conv,)
            ).fetchone()
            if row:
                title = row['title'] or '(untitled)'
                msg_count = row['msg_count'] or 0
                lines.append(f'\nTarget conversation: "{title}" ({msg_count} messages)')
                # Show last 2 messages briefly
                try:
                    msgs = json.loads(row['messages'] or '[]')
                    for m in msgs[-2:]:
                        role = m.get('role', '?')
                        content = (m.get('content') or '')[:200]
                        if isinstance(content, list):
                            content = '[multimodal]'
                        lines.append(f'  [{role}] {content}')
                except (json.JSONDecodeError, TypeError) as e:
                    logger.debug('[Proactive] Failed to parse conv messages: %s', e)
            else:
                lines.append(f'\nTarget conversation {target_conv[:12]} not found.')
        except Exception as e:
            logger.debug('[Proactive] Failed to gather conv status: %s', e)
            lines.append('\nTarget conversation status unavailable.')

    lines.append(f'\nSystem time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    lines.append(f'Execution count so far: {task.get("execution_count", 0)}')

    max_exec = task.get('max_executions', 0)
    if max_exec > 0:
        lines.append(f'Max executions: {max_exec}')

    return '\n'.join(lines)


# ── Poll decision ───────────────────────────────────────────────────────────

_POLL_SYSTEM_PROMPT = """You are a proactive scheduler agent. Your job is to decide whether to ACT NOW based on a standing instruction and a current system status report.

Rules:
- Respond ONLY with valid JSON: {"act": true/false, "reason": "brief explanation"}
- act=true means the conditions appear met (or it's time for a scheduled action)
- act=false means conditions are not yet met, wait for next poll
- If unsure but conditions seem close, prefer act=true (better to check than miss)
- Keep your reason under 100 characters
- This is poll-only — you cannot use tools here"""


def poll_decision(task: dict[str, Any]) -> tuple[bool, str, int]:
    """Run a lightweight LLM poll to decide whether to act.

    Args:
        task: The scheduled task dict with task_type='agent'.

    Returns:
        (should_act, reason, tokens_used)
    """
    from lib.llm_dispatch import smart_chat

    instruction = task.get('command', '')
    status = gather_system_status(task)

    messages = [
        {'role': 'system', 'content': _POLL_SYSTEM_PROMPT},
        {'role': 'user', 'content': (
            f'YOUR STANDING INSTRUCTION:\n{instruction}\n\n'
            f'CURRENT STATUS:\n{status}\n\n'
            f'Should I act now? Respond with JSON: {{"act": true/false, "reason": "..."}}'
        )},
    ]

    try:
        with log_context('proactive_poll', logger=logger):
            content, usage = smart_chat(
                messages,
                max_tokens=256,
                temperature=0,
                capability='cheap',
                log_prefix=f'[Proactive:{task["id"][:8]}]',
            )
    except Exception as e:
        logger.error('[Proactive:%s] Poll LLM call failed: %s', task['id'][:8], e, exc_info=True)
        return False, f'LLM error: {e}', 0

    tokens_used = 0
    if isinstance(usage, dict):
        tokens_used = usage.get('total_tokens', 0)

    # Parse the LLM's JSON decision
    try:
        # Strip markdown code fences if present
        text = content.strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

        decision = json.loads(text)
        should_act = bool(decision.get('act', False))
        reason = str(decision.get('reason', ''))[:200]
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning('[Proactive:%s] Failed to parse poll response: %s — raw: %.500s',
                       task['id'][:8], e, content)
        # If we can't parse, default to skip
        should_act = False
        reason = f'Parse error: {content[:100]}'

    return should_act, reason, tokens_used


# ── Record poll decision ────────────────────────────────────────────────────

def record_poll(task_id: str, decision: str, reason: str, model: str,
                tokens_used: int, status_snapshot: str,
                execution_task_id: str = '') -> None:
    """Write a poll decision to the proactive_poll_log table."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        now = datetime.now().isoformat()
        db.execute(
            '''INSERT INTO proactive_poll_log
               (task_id, poll_time, decision, reason, status_snapshot, model, tokens_used, execution_task_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)''',
            [task_id, now, decision, reason[:500], status_snapshot[:5000], model, tokens_used, execution_task_id]
        )
        db.commit()
    except Exception as e:
        logger.warning('[Proactive] Failed to record poll for task %s: %s', task_id, e, exc_info=True)


def get_poll_log(task_id: str, limit: int = 30) -> list[dict]:
    """Retrieve recent poll log entries for a task."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        rows = db.execute(
            'SELECT * FROM proactive_poll_log WHERE task_id=? ORDER BY poll_time DESC LIMIT ?',
            [task_id, limit]
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning('[Proactive] Failed to get poll log for task %s: %s', task_id, e, exc_info=True)
        return []


# ── Execute proactive task ──────────────────────────────────────────────────

def execute_proactive_task(task: dict[str, Any]) -> str | None:
    """Create a real agentic task in the target conversation and run it.

    This creates a user message (tagged as proactive) in the conversation,
    then creates a task via create_task() and runs it with full tool access.
    The execution is visible in the frontend as a normal assistant response.

    Args:
        task: The scheduled task dict.

    Returns:
        The task_id of the created agentic task, or None on failure.
    """
    import threading

    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
    from lib.tasks_pkg import run_task
    from lib.tasks_pkg.manager import create_task as create_agentic_task

    task_id_short = task['id'][:8]
    target_conv = task.get('target_conv_id', '')
    instruction = task.get('command', '')
    poll_count = task.get('poll_count', 0)

    if not target_conv:
        logger.error('[Proactive:%s] No target_conv_id — cannot execute', task_id_short)
        return None

    if not instruction:
        logger.error('[Proactive:%s] No instruction (command) — cannot execute', task_id_short)
        return None

    logger.info('[Proactive:%s] 🚀 Executing agent task in conv=%s (poll #%d triggered)',
                task_id_short, target_conv[:12], poll_count)

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # ── 1. Load conversation messages ──
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (target_conv,)
        ).fetchone()

        if not row:
            logger.error('[Proactive:%s] Target conversation %s not found in DB', task_id_short, target_conv)
            return None

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError):
            messages = []

        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError):
            settings = {}

        # ── 2. Append proactive user message ──
        proactive_user_msg = {
            'role': 'user',
            'content': (
                f'⏰ **[Proactive Agent — Poll #{poll_count + 1}]** '
                f'"{task["name"]}"\n\n'
                f'{instruction}'
            ),
            'timestamp': datetime.now().isoformat(),
            '_proactive': True,
            '_proactiveTaskId': task['id'],
        }
        messages.append(proactive_user_msg)

        # ── 3. Append placeholder assistant message ──
        assistant_msg = {
            'role': 'assistant',
            'content': '',
            'thinking': '',
            'timestamp': datetime.now().isoformat(),
            '_proactive': True,
        }
        messages.append(assistant_msg)

        # ── 4. Write messages back to DB ──
        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db,
            """UPDATE conversations SET messages=?, updated_at=?, msg_count=?, search_text=?,
                   search_tsv=to_tsvector('simple', left(?, 50000))
               WHERE id=? AND user_id=1""",
            (messages_json, now_ms, len(messages), search_text, search_text, target_conv)
        )

        # ── 5. Build config from the stored tools_config ──
        try:
            tools_cfg = json.loads(task.get('tools_config', '{}') or '{}')
        except (json.JSONDecodeError, TypeError):
            tools_cfg = {}

        config = {
            'model': settings.get('model') or tools_cfg.get('model', ''),
            'preset': settings.get('model') or tools_cfg.get('model', ''),
            'thinkingEnabled': True,
            'searchMode': tools_cfg.get('searchMode', settings.get('searchMode', 'multi')),
            'fetchEnabled': True,  # always on
            'projectPath': tools_cfg.get('projectPath', settings.get('projectPath', '')),
            'codeExecEnabled': tools_cfg.get('codeExecEnabled', settings.get('codeExecEnabled', False)),
            'browserEnabled': tools_cfg.get('browserEnabled', settings.get('browserEnabled', False)),
            'skillsEnabled': tools_cfg.get('skillsEnabled', settings.get('skillsEnabled', True)),
            'swarmEnabled': tools_cfg.get('swarmEnabled', settings.get('swarmEnabled', False)),
            'imageGenEnabled': tools_cfg.get('imageGenEnabled', settings.get('imageGenEnabled', False)),
            'schedulerEnabled': True,  # scheduler tools remain available
        }

        # ── 6. Create the agentic task ──
        agentic_task = create_agentic_task(target_conv, messages, config)
        agentic_task_id = agentic_task['id']

        # Write activeTaskId into conversation settings so frontend can discover it
        settings['activeTaskId'] = agentic_task_id
        settings_json = json.dumps(settings, ensure_ascii=False)
        db_execute_with_retry(db,
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
            (settings_json, target_conv)
        )

        logger.info('[Proactive:%s] Created agentic task %s in conv=%s, starting execution thread',
                    task_id_short, agentic_task_id[:8], target_conv[:12])

        # ── 7. Run the task in a background thread ──
        def _run():
            try:
                run_task(agentic_task)
            except Exception as e:
                logger.error('[Proactive:%s] Agentic task %s execution failed: %s',
                             task_id_short, agentic_task_id[:8], e, exc_info=True)

        threading.Thread(target=_run, daemon=True, name=f'proactive-{agentic_task_id[:8]}').start()
        return agentic_task_id

    except Exception as e:
        logger.error('[Proactive:%s] Failed to execute agent task: %s', task_id_short, e, exc_info=True)
        return None


# ── Check if task is currently executing ────────────────────────────────────

def is_task_executing(task: dict[str, Any]) -> bool:
    """Check if this proactive task has an execution still running."""
    last_exec_task_id = task.get('last_execution_task_id', '')
    if not last_exec_task_id:
        return False

    from lib.tasks_pkg.manager import tasks, tasks_lock
    with tasks_lock:
        t = tasks.get(last_exec_task_id)
        if t and t.get('status') == 'running':
            return True
    return False


# ── Check expiration / max executions ───────────────────────────────────────

def should_auto_disable(task: dict[str, Any]) -> bool:
    """Check if a proactive task should be auto-disabled."""
    # Max executions reached
    max_exec = task.get('max_executions', 0)
    if max_exec > 0 and task.get('execution_count', 0) >= max_exec:
        logger.info('[Proactive:%s] Auto-disabling: max_executions=%d reached',
                    task['id'][:8], max_exec)
        return True

    # Expired
    expires_at = task.get('expires_at', '')
    if expires_at:
        try:
            exp = datetime.fromisoformat(expires_at)
            if datetime.now() >= exp:
                logger.info('[Proactive:%s] Auto-disabling: expired at %s', task['id'][:8], expires_at)
                return True
        except (ValueError, TypeError) as e:
            logger.debug('[Proactive] expires_at parse error: %s', e)

    return False


__all__ = [
    'gather_system_status', 'poll_decision', 'record_poll', 'get_poll_log',
    'execute_proactive_task', 'is_task_executing', 'should_auto_disable',
]
