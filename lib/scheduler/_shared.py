"""lib/scheduler/_shared.py — Shared utilities for timer and proactive subsystems.

Extracted to eliminate duplication between ``timer._execute_continuation()``
and ``proactive.execute_proactive_task()``, which both follow the same
seven-step sequence:

  1. Load conversation messages + settings from DB
  2. Append a caller-provided user message
  3. Append a placeholder assistant message
  4. Write messages back with full-text search indexing
  5. Build an agentic task config from tools_config + conversation settings
  6. Create the agentic task and set ``activeTaskId``
  7. Run the task in a background daemon thread
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from typing import Any

from lib.log import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════════════════
#  Config builder
# ═════════════════════════════════════════════════════════════════════════════

def build_task_config(tools_config: dict, conv_settings: dict) -> dict:
    """Build an agentic task config by merging tools_config with conversation settings.

    ``tools_config`` (from the timer / proactive task definition) takes
    precedence; ``conv_settings`` (from the target conversation) provides
    fallback values.

    Args:
        tools_config: Tool settings stored on the timer or scheduled task.
        conv_settings: Settings dict from the target conversation row.

    Returns:
        Config dict suitable for ``create_task()``.
    """
    return {
        'model': conv_settings.get('model') or tools_config.get('model', ''),
        'preset': conv_settings.get('model') or tools_config.get('model', ''),
        'thinkingEnabled': True,
        'searchMode': tools_config.get('searchMode', conv_settings.get('searchMode', 'multi')),
        'fetchEnabled': True,
        'projectPath': tools_config.get('projectPath', conv_settings.get('projectPath', '')),
        'codeExecEnabled': tools_config.get('codeExecEnabled', conv_settings.get('codeExecEnabled', False)),
        'browserEnabled': tools_config.get('browserEnabled', conv_settings.get('browserEnabled', False)),
        'memoryEnabled': tools_config.get('memoryEnabled', conv_settings.get('memoryEnabled', True)),
        'swarmEnabled': tools_config.get('swarmEnabled', conv_settings.get('swarmEnabled', False)),
        'imageGenEnabled': tools_config.get('imageGenEnabled', conv_settings.get('imageGenEnabled', False)),
        'schedulerEnabled': True,
    }


# ═════════════════════════════════════════════════════════════════════════════
#  Inject user message + start agentic task
# ═════════════════════════════════════════════════════════════════════════════

def inject_and_run_task(
    conv_id: str,
    user_message: dict[str, Any],
    tools_config_json: str | dict,
    log_prefix: str = '',
) -> str | None:
    """Load conversation, inject messages, and start an agentic task.

    This is the shared execution core used by both *timer continuation*
    and *proactive agent execution*.

    Args:
        conv_id: Target conversation ID.
        user_message: Complete user message dict (must include ``role``,
            ``content``, ``timestamp``, and any domain-specific tags like
            ``_timer`` or ``_proactive``).
        tools_config_json: Tool configuration — JSON string **or**
            already-parsed dict.
        log_prefix: Logging prefix for traceability
            (e.g. ``'[Timer:tmr_abc123]'``).

    Returns:
        The agentic ``task_id`` on success, or ``None`` on failure.
    """
    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
    from lib.tasks_pkg import run_task
    from lib.tasks_pkg.manager import create_task as create_agentic_task

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # 1. Load conversation ────────────────────────────────────────
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()

        if not row:
            logger.error('%s Conversation %s not found', log_prefix, conv_id)
            return None

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('%s Failed to parse conv messages, defaulting to []: %s',
                         log_prefix, e)
            messages = []

        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('%s Failed to parse conv settings, defaulting to {}: %s',
                         log_prefix, e)
            settings = {}

        # 2. Append caller-provided user message ─────────────────────
        messages.append(user_message)

        # 3. Append placeholder assistant message ────────────────────
        assistant_msg: dict[str, Any] = {
            'role': 'assistant',
            'content': '',
            'thinking': '',
            'timestamp': datetime.now().isoformat(),
        }
        # Propagate source tags so the frontend can style them
        for tag in ('_timer', '_proactive'):
            if user_message.get(tag):
                assistant_msg[tag] = True
        messages.append(assistant_msg)

        # 4. Write messages back to DB ───────────────────────────────
        from routes.conversations import build_search_text

        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db,
            """UPDATE conversations SET messages=?, updated_at=?, msg_count=?,
                   search_text=?
               WHERE id=? AND user_id=1""",
            (messages_json, now_ms, len(messages),
             search_text, conv_id)
        )
        # Update FTS5 index
        if search_text:
            try:
                db.execute(
                    "INSERT OR REPLACE INTO conversations_fts (rowid, search_text) "
                    "SELECT rowid, ? FROM conversations WHERE id = ?",
                    (search_text, conv_id)
                )
                db.commit()
            except Exception as _fts_err:
                logger.debug('[Scheduler] FTS update failed (non-fatal): %s', _fts_err)

        # 5. Build config ────────────────────────────────────────────
        if isinstance(tools_config_json, str):
            try:
                tools_cfg = json.loads(tools_config_json or '{}')
            except (json.JSONDecodeError, TypeError) as e:
                logger.debug('%s Failed to parse tools_config, defaulting to {}: %s',
                             log_prefix, e)
                tools_cfg = {}
        else:
            tools_cfg = tools_config_json or {}

        config = build_task_config(tools_cfg, settings)

        # 6. Create agentic task + set activeTaskId ──────────────────
        agentic_task = create_agentic_task(conv_id, messages, config)
        agentic_task_id = agentic_task['id']

        settings['activeTaskId'] = agentic_task_id
        settings_json = json.dumps(settings, ensure_ascii=False)
        db_execute_with_retry(db,
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
            (settings_json, conv_id)
        )

        logger.info('%s Created agentic task %s in conv=%s',
                     log_prefix, agentic_task_id[:8], conv_id[:12])

        # 7. Run in background daemon thread ─────────────────────────
        def _run():
            try:
                run_task(agentic_task)
            except Exception as e:
                logger.error('%s Agentic task %s execution failed: %s',
                             log_prefix, agentic_task_id[:8], e, exc_info=True)

        threading.Thread(
            target=_run, daemon=True,
            name=f'sched-exec-{agentic_task_id[:8]}',
        ).start()

        return agentic_task_id

    except Exception as e:
        logger.error('%s Failed to inject and run task: %s',
                     log_prefix, e, exc_info=True)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  JSON decision parser
# ═════════════════════════════════════════════════════════════════════════════

def parse_json_decision(content: str | None, key: str = 'ready') -> tuple[bool, str]:
    """Parse a JSON boolean decision from LLM content.

    Handles common LLM quirks: markdown code fences, extra whitespace.

    Args:
        content: Raw LLM response text.
        key: JSON key for the boolean decision — ``'ready'`` for timers,
            ``'act'`` for proactive agents.

    Returns:
        ``(decision_bool, reason_string)``

    Raises:
        json.JSONDecodeError: If the content cannot be parsed as JSON.
        TypeError: If the content is not a string.
    """
    text = (content or '').strip()
    # Strip markdown code fences (```json ... ```)
    if text.startswith('```'):
        text = text.split('\n', 1)[-1]
        if text.endswith('```'):
            text = text[:-3]
        text = text.strip()

    decision = json.loads(text)
    return bool(decision.get(key, False)), str(decision.get('reason', ''))[:200]
