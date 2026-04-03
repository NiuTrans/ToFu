"""lib/scheduler/timer.py — Timer Watcher: async poll → decide → continue.

The Timer Watcher is a simplified, conversation-inline variant of the
proactive agent.  An agent tool call creates a timer; a background thread
polls independently until conditions are met, then injects a follow-up
user message and kicks off a new agentic task.

Key design decisions:
  • Each poll is *independent* — no cross-poll history (token-saving).
  • The poll optionally runs a shell command first and feeds its output
    to the LLM for grounded decision-making.
  • Single-shot by default (auto-cancels after triggering).
  • Timer threads are daemon threads so they don't block server shutdown.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

from lib.log import get_logger, log_context

logger = get_logger(__name__)

# ── In-memory registry of active timer threads ──────────────────────────────

_active_timers: dict[str, threading.Thread] = {}
_timers_lock = threading.Lock()

# ── Per-timer cache of last check_command output for early-exit filtering ────
# If the command output hasn't changed since the last poll, we skip the LLM
# call entirely — saves tokens and reduces frontend noise.
_last_cmd_outputs: dict[str, str] = {}
_cmd_outputs_lock = threading.Lock()


# ═════════════════════════════════════════════════════════════════════════════
#  CRUD
# ═════════════════════════════════════════════════════════════════════════════

def create_timer(conv_id: str,
                 check_instruction: str,
                 continuation_message: str,
                 poll_interval: int = 60,
                 max_polls: int = 120,
                 check_command: str = '',
                 tools_config: dict | None = None,
                 source_task_id: str = '') -> dict[str, Any]:
    """Create a timer watcher and persist to DB.

    Args:
        conv_id: Conversation to inject the continuation into.
        check_instruction: Natural-language instruction for the LLM poll.
        continuation_message: The user message to inject when ready.
        poll_interval: Seconds between polls (minimum 10).
        max_polls: Maximum number of polls before exhaustion (0=unlimited).
        check_command: Optional shell command to run before each poll.
        tools_config: Tool settings for the continuation task.
        source_task_id: The task that created this timer.

    Returns:
        Timer record dict.
    """
    from lib.database import DOMAIN_SYSTEM, get_thread_db

    timer_id = 'tmr_' + str(uuid.uuid4())[:8]
    now = datetime.now().isoformat()
    poll_interval = max(poll_interval, 10)  # floor at 10s

    db = get_thread_db(DOMAIN_SYSTEM)
    db.execute(
        '''INSERT INTO timer_watchers
           (id, conv_id, source_task_id, check_instruction, check_command,
            continuation_message, poll_interval, max_polls, status,
            tools_config, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)''',
        [timer_id, conv_id, source_task_id, check_instruction, check_command,
         continuation_message, poll_interval, max_polls,
         json.dumps(tools_config or {}, ensure_ascii=False), now, now]
    )
    db.commit()

    timer = _get_timer_row(timer_id)
    logger.info('[Timer:%s] Created — conv=%s poll_interval=%ds max_polls=%d check_cmd=%s',
                timer_id, conv_id[:12], poll_interval, max_polls,
                (check_command[:80] + '…') if len(check_command) > 80 else check_command or '(none)')
    return timer


def cancel_timer(timer_id: str) -> bool:
    """Cancel an active timer."""
    from lib.database import DOMAIN_SYSTEM, get_thread_db

    db = get_thread_db(DOMAIN_SYSTEM)
    now = datetime.now().isoformat()
    db.execute(
        "UPDATE timer_watchers SET status='cancelled', cancelled_at=?, updated_at=? WHERE id=? AND status='active'",
        [now, now, timer_id]
    )
    db.commit()

    # Signal the background thread to stop
    with _timers_lock:
        _active_timers.pop(timer_id, None)
    with _cmd_outputs_lock:
        _last_cmd_outputs.pop(timer_id, None)

    logger.info('[Timer:%s] Cancelled', timer_id)
    return True


def force_trigger_timer(timer_id: str) -> str | None:
    """Force-trigger a timer, skipping the poll.

    Returns:
        The execution task_id, or None on failure.
    """
    timer = get_timer(timer_id)
    if not timer:
        return None
    if timer['status'] != 'active':
        logger.warning('[Timer:%s] Cannot trigger — status=%s', timer_id, timer['status'])
        return None

    return _execute_continuation(timer)


def get_timer(timer_id: str) -> dict[str, Any] | None:
    """Get a single timer by ID."""
    return _get_timer_row(timer_id)


def list_active_timers() -> list[dict[str, Any]]:
    """Return all timers (active first, then recent triggered/cancelled)."""
    from lib.database import DOMAIN_SYSTEM, get_thread_db
    db = get_thread_db(DOMAIN_SYSTEM)
    rows = db.execute(
        '''SELECT * FROM timer_watchers
           ORDER BY CASE status WHEN 'active' THEN 0 ELSE 1 END,
                    created_at DESC
           LIMIT 50'''
    ).fetchall()
    return [dict(r) for r in rows]


def get_timer_poll_log(timer_id: str, limit: int = 30) -> list[dict]:
    """Retrieve recent poll log entries for a timer."""
    from lib.database import DOMAIN_SYSTEM, get_thread_db
    try:
        db = get_thread_db(DOMAIN_SYSTEM)
        rows = db.execute(
            'SELECT * FROM timer_poll_log WHERE timer_id=? ORDER BY poll_time DESC LIMIT ?',
            [timer_id, limit]
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.warning('[Timer] Failed to get poll log for %s: %s', timer_id, e, exc_info=True)
        return []


def _get_timer_row(timer_id: str) -> dict[str, Any] | None:
    """Fetch a timer record from DB."""
    from lib.database import DOMAIN_SYSTEM, get_thread_db
    db = get_thread_db(DOMAIN_SYSTEM)
    row = db.execute('SELECT * FROM timer_watchers WHERE id=?', [timer_id]).fetchone()
    return dict(row) if row else None


# ═════════════════════════════════════════════════════════════════════════════
#  Poll logic
# ═════════════════════════════════════════════════════════════════════════════

_POLL_SYSTEM_PROMPT = """You are a timer watcher. Your job is to decide whether conditions are met based on a check instruction and optional command output.

You have access to tools (web_search, fetch_url, run_command, list_dir, read_files, grep_search, find_files, etc.) to actively gather information when needed. Use them when the check instruction requires more than what the command output provides.

Rules:
- After gathering information, respond with ONLY valid JSON: {"ready": true/false, "reason": "brief explanation"}
- ready=true means conditions are met and the follow-up task should start
- ready=false means conditions are NOT yet met, keep waiting
- Keep your reason under 100 characters
- Do NOT think — go straight to action or decision
- Minimize tool calls — only use tools when the check_command output is insufficient"""

# Maximum LLM rounds per poll (tool calls + final decision)
_MAX_POLL_AGENT_ROUNDS = 5


def _run_check_command(check_command: str, timer_id: str) -> str:
    """Run the optional shell check command and return its output.

    Returns:
        Command stdout+stderr (truncated to 4000 chars), or error message.
    """
    if not check_command.strip():
        return ''

    try:
        from lib.compat import get_shell_args
        result = subprocess.run(
            get_shell_args(check_command),
            capture_output=True, text=True,
            timeout=30,
        )
        output = result.stdout[:3500]
        if result.stderr:
            output += f'\n[stderr] {result.stderr[:500]}'
        return output.strip()
    except subprocess.TimeoutExpired:
        logger.warning('[Timer:%s] Check command timed out after 30s: %.100s',
                       timer_id, check_command)
        return '(check command timed out after 30s)'
    except Exception as e:
        logger.warning('[Timer:%s] Check command failed: %s', timer_id, e)
        return f'(check command error: {e})'


def _build_poll_tools(tools_config: dict) -> list | None:
    """Build a tool list for the timer poll based on the stored tools_config.

    Returns a list of tool definitions or None if no tools should be available.
    The timer poll gets the same tools as the main agent (project tools,
    search, fetch, code_exec) except for human interaction tools (ask_human,
    emit_to_user, scheduler, swarm, skills).
    """
    try:
        from lib.tools import (
            CODE_EXEC_TOOL,
            FETCH_URL_TOOL,
            PROJECT_TOOL_READ_LOCAL_FILE,
            PROJECT_TOOLS,
            SEARCH_TOOL_MULTI,
        )

        tool_list = []
        project_path = tools_config.get('projectPath', '')
        project_enabled = bool(project_path)

        # ★ Search + Fetch — almost always useful
        search_mode = tools_config.get('searchMode', 'multi')
        if search_mode:
            tool_list.append(SEARCH_TOOL_MULTI)
        if tools_config.get('fetchEnabled', True) or search_mode:
            tool_list.append(FETCH_URL_TOOL)

        # ★ Project tools — file operations on the project
        if project_enabled:
            tool_list.extend(t for t in PROJECT_TOOLS
                             if t is not PROJECT_TOOL_READ_LOCAL_FILE)
        elif tools_config.get('codeExecEnabled', False):
            tool_list.append(CODE_EXEC_TOOL)

        # ★ Local file reader — always available
        tool_list.append(PROJECT_TOOL_READ_LOCAL_FILE)

        # ★ Browser tools
        if tools_config.get('browserEnabled', False):
            try:
                from lib.browser import is_extension_connected
                if is_extension_connected():
                    from lib.browser.advanced import ADVANCED_BROWSER_TOOLS
                    from lib.tools import BROWSER_TOOLS
                    tool_list.extend(BROWSER_TOOLS)
                    tool_list.extend(ADVANCED_BROWSER_TOOLS)
            except Exception as e:
                logger.debug('[Timer] Browser tools skipped: %s', e)

        # ★ Image generation
        if tools_config.get('imageGenEnabled', False):
            try:
                from lib.tools.image_gen import GENERATE_IMAGE_TOOL
                tool_list.append(GENERATE_IMAGE_TOOL)
            except Exception as e:
                logger.debug('[Timer] Image gen tool skipped: %s', e)

        return tool_list if tool_list else None

    except Exception as e:
        logger.warning('[Timer] Failed to build poll tools: %s', e, exc_info=True)
        return None


def _execute_poll_tool(tool_call: dict, timer_id: str,
                       project_path: str) -> str:
    """Execute a single tool call within a timer poll.

    Uses the same _execute_tool_one dispatcher as the main agent and swarm
    sub-agents, but with a minimal task_proxy (no SSE events needed).

    Args:
        tool_call: The tool call dict from the LLM response.
        timer_id: For logging.
        project_path: Project path from tools_config.

    Returns:
        Tool result string (truncated to 8000 chars).
    """
    import threading as _threading

    fn_info = tool_call.get('function', {})
    fn_name = fn_info.get('name', '?')
    fn_args_raw = fn_info.get('arguments', '{}')
    t0 = time.time()

    logger.debug('[Timer:%s] Tool call: %s args=%.300s', timer_id, fn_name, fn_args_raw)

    try:
        fn_args = json.loads(fn_args_raw) if isinstance(fn_args_raw, str) else fn_args_raw
    except json.JSONDecodeError:
        try:
            from lib.utils import repair_json as _repair_json
            fn_args = _repair_json(fn_args_raw if isinstance(fn_args_raw, str) else '{}')
        except Exception as e:
            logger.warning('[Timer:%s] Invalid JSON args for %s: %s', timer_id, fn_name, e)
            return f'Invalid JSON arguments for {fn_name}: {fn_args_raw[:200]}'

    if not isinstance(fn_args, dict):
        fn_args = {}

    try:
        from lib.tasks_pkg.executor import _execute_tool_one

        # Build minimal task proxy — no SSE events needed for timer polls
        task_proxy = {
            'id': timer_id,
            'convId': '',
            'status': 'running',
            'events': [],
            'events_lock': _threading.Lock(),
            'searchRounds': [],
            'phase': None,
        }

        tc_id = tool_call.get('id', uuid.uuid4().hex[:8])
        round_entry = {
            'roundNum': 0,
            'query': f'{fn_name}({str(fn_args)[:60]})',
            'results': None,
            'status': 'searching',
            'toolName': fn_name,
        }
        cfg = {'model': '', 'thinking_enabled': False, 'search_mode': 'multi'}
        project_enabled = bool(project_path)

        _, tool_content, _ = _execute_tool_one(
            task_proxy, tool_call, fn_name, tc_id, fn_args,
            0, round_entry, cfg, project_path, project_enabled,
        )

        result = str(tool_content) if tool_content is not None else ''
        # Truncate to prevent context blowup in the poll
        if len(result) > 8000:
            result = result[:6000] + f'\n\n... [TRUNCATED: {len(result):,} → 8,000 chars]' + result[-1500:]
        elapsed = time.time() - t0
        logger.debug('[Timer:%s] Tool %s completed in %.2fs result_len=%d',
                     timer_id, fn_name, elapsed, len(result))
        return result

    except Exception as e:
        elapsed = time.time() - t0
        logger.warning('[Timer:%s] Tool %s FAILED in %.2fs: %s',
                       timer_id, fn_name, elapsed, e, exc_info=True)
        return f'Tool error ({fn_name}): {type(e).__name__}: {e}'


def poll_timer(timer_id: str) -> tuple[bool, str, int, bool]:
    """Run a single independent poll for a timer.

    The poll runs as a mini-agent loop with tool access:
    1. Build tools from timer's tools_config
    2. Call LLM with tools
    3. If LLM returns tool_calls, execute them and loop
    4. When LLM returns content (JSON decision), parse and return

    After running the check_command, compares its output against the
    previous poll.  If the output is identical (non-empty), the LLM
    call is skipped entirely — saving tokens and frontend noise.

    Args:
        timer_id: The timer to poll.

    Returns:
        (ready, reason, tokens_used, skipped)
        *skipped* is True when the LLM call was elided because the
        check_command output was unchanged.
    """
    from lib.llm_dispatch import smart_chat

    timer = _get_timer_row(timer_id)
    if not timer or timer['status'] != 'active':
        return False, 'Timer no longer active', 0, False

    check_instruction = timer['check_instruction']
    check_command = timer.get('check_command', '')

    # Optionally run the check command for grounded data
    cmd_output = _run_check_command(check_command, timer_id)

    # ── Early-exit: skip LLM if command output is unchanged ──────────
    if cmd_output:
        with _cmd_outputs_lock:
            prev_output = _last_cmd_outputs.get(timer_id)
        if prev_output is not None and cmd_output == prev_output:
            logger.debug('[Timer:%s] Check command output unchanged (%d chars) — skipping LLM',
                         timer_id, len(cmd_output))
            return False, '', 0, True
        # Cache current output for next comparison
        with _cmd_outputs_lock:
            _last_cmd_outputs[timer_id] = cmd_output

    # ── Build tool list from timer's tools_config ────────────────────
    try:
        tools_config = json.loads(timer.get('tools_config', '{}') or '{}')
    except (json.JSONDecodeError, TypeError) as e:
        logger.debug('[Timer:%s] Failed to parse tools_config: %s', timer_id, e)
        tools_config = {}

    poll_tools = _build_poll_tools(tools_config)
    project_path = tools_config.get('projectPath', '')

    # ── Build initial messages ───────────────────────────────────────
    user_content_parts = [f'CHECK INSTRUCTION:\n{check_instruction}']
    if cmd_output:
        user_content_parts.append(f'\nCOMMAND OUTPUT (from: {check_command[:100]}):\n{cmd_output}')
    user_content_parts.append(f'\nCurrent time: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}')
    user_content_parts.append(f'Poll #{timer.get("poll_count", 0) + 1}')
    user_content_parts.append('\nAre conditions met? Respond with JSON: {"ready": true/false, "reason": "..."}')

    messages = [
        {'role': 'system', 'content': _POLL_SYSTEM_PROMPT},
        {'role': 'user', 'content': '\n'.join(user_content_parts)},
    ]

    total_tokens = 0

    # ── Mini-agent loop: LLM call → tool execution → repeat ─────────
    for agent_round in range(_MAX_POLL_AGENT_ROUNDS):
        try:
            with log_context('timer_poll', logger=logger):
                content, usage = smart_chat(
                    messages,
                    max_tokens=4096 if poll_tools else 256,
                    temperature=0,
                    thinking_enabled=False,
                    tools=poll_tools,
                    capability='cheap',
                    log_prefix=f'[Timer:{timer_id}:R{agent_round}]',
                )
        except Exception as e:
            logger.error('[Timer:%s] Poll LLM call failed (round %d): %s',
                         timer_id, agent_round, e, exc_info=True)
            return False, f'LLM error: {e}', total_tokens, False

        if isinstance(usage, dict):
            total_tokens += usage.get('total_tokens', 0)

        # ── Check for tool calls ─────────────────────────────────────
        tool_calls = usage.get('_tool_calls', []) if isinstance(usage, dict) else []

        if tool_calls:
            logger.info('[Timer:%s] Round %d: %d tool call(s) → %s',
                        timer_id, agent_round,
                        len(tool_calls),
                        [tc.get('function', {}).get('name', '?') for tc in tool_calls])

            # Append assistant message with tool_calls (no content)
            messages.append({
                'role': 'assistant',
                'content': content or None,
                'tool_calls': tool_calls,
            })

            # Execute each tool call and append results
            for tc in tool_calls:
                tc_id = tc.get('id', uuid.uuid4().hex[:8])
                result = _execute_poll_tool(tc, timer_id, project_path)
                messages.append({
                    'role': 'tool',
                    'tool_call_id': tc_id,
                    'content': result,
                })

            # Continue the loop — LLM will process tool results
            continue

        # ── No tool calls → parse JSON decision from content ─────────
        break

    # Parse JSON decision from final content
    try:
        text = (content or '').strip()
        if text.startswith('```'):
            text = text.split('\n', 1)[-1]
            if text.endswith('```'):
                text = text[:-3]
            text = text.strip()

        decision = json.loads(text)
        ready = bool(decision.get('ready', False))
        reason = str(decision.get('reason', ''))[:200]
    except (json.JSONDecodeError, TypeError, AttributeError) as e:
        logger.warning('[Timer:%s] Failed to parse poll response: %s — raw: %.500s',
                       timer_id, e, content)
        ready = False
        reason = f'Parse error: {(content or "")[:100]}'

    return ready, reason, total_tokens, False


def _record_poll(timer_id: str, decision: str, reason: str,
                 tokens_used: int, check_output: str = '') -> None:
    """Write a poll decision to the timer_poll_log table."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        now = datetime.now().isoformat()
        db.execute(
            '''INSERT INTO timer_poll_log
               (timer_id, poll_time, decision, reason, check_output, tokens_used)
               VALUES (?, ?, ?, ?, ?, ?)''',
            [timer_id, now, decision, reason[:500], check_output[:5000], tokens_used]
        )
        db.commit()
    except Exception as e:
        logger.warning('[Timer:%s] Failed to record poll: %s', timer_id, e, exc_info=True)


# ═════════════════════════════════════════════════════════════════════════════
#  Continuation execution — inject user message + start agentic task
# ═════════════════════════════════════════════════════════════════════════════

def _execute_continuation(timer: dict[str, Any]) -> str | None:
    """Inject user message and start agentic task in the target conversation.

    Args:
        timer: The timer record dict.

    Returns:
        The agentic task_id, or None on failure.
    """
    import threading as _threading

    from lib.database import DOMAIN_CHAT, db_execute_with_retry, get_thread_db, json_dumps_pg
    from lib.tasks_pkg import run_task
    from lib.tasks_pkg.manager import create_task as create_agentic_task

    timer_id = timer['id']
    conv_id = timer['conv_id']
    continuation_msg = timer['continuation_message']

    logger.info('[Timer:%s] 🚀 Executing continuation in conv=%s', timer_id, conv_id[:12])

    try:
        db = get_thread_db(DOMAIN_CHAT)

        # 1. Load conversation
        row = db.execute(
            'SELECT messages, settings FROM conversations WHERE id=? AND user_id=1',
            (conv_id,)
        ).fetchone()

        if not row:
            logger.error('[Timer:%s] Conversation %s not found', timer_id, conv_id)
            return None

        try:
            messages = json.loads(row['messages'] or '[]')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Timer:%s] Failed to parse conv messages, defaulting to []: %s', timer_id, e)
            messages = []

        try:
            settings = json.loads(row['settings'] or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Timer:%s] Failed to parse conv settings, defaulting to {}: %s', timer_id, e)
            settings = {}

        # 2. Append timer user message
        timer_user_msg = {
            'role': 'user',
            'content': (
                f'⏱️ **[Timer Watcher Triggered — {timer_id}]**\n\n'
                f'{continuation_msg}'
            ),
            'timestamp': datetime.now().isoformat(),
            '_timer': True,
            '_timerId': timer_id,
        }
        messages.append(timer_user_msg)

        # 3. Append placeholder assistant message
        assistant_msg = {
            'role': 'assistant',
            'content': '',
            'thinking': '',
            'timestamp': datetime.now().isoformat(),
            '_timer': True,
        }
        messages.append(assistant_msg)

        # 4. Write messages back
        from routes.conversations import build_search_text
        messages_json = json_dumps_pg(messages)
        search_text = build_search_text(messages)
        now_ms = int(time.time() * 1000)
        db_execute_with_retry(db,
            """UPDATE conversations SET messages=?, updated_at=?, msg_count=?, search_text=?,
                   search_tsv=to_tsvector('simple', left(?, 50000))
               WHERE id=? AND user_id=1""",
            (messages_json, now_ms, len(messages), search_text, search_text, conv_id)
        )

        # 5. Build config from tools_config
        try:
            tools_cfg = json.loads(timer.get('tools_config', '{}') or '{}')
        except (json.JSONDecodeError, TypeError) as e:
            logger.debug('[Timer:%s] Failed to parse tools_config, defaulting to {}: %s', timer_id, e)
            tools_cfg = {}

        config = {
            'model': settings.get('model') or tools_cfg.get('model', ''),
            'preset': settings.get('model') or tools_cfg.get('model', ''),
            'thinkingEnabled': True,
            'searchMode': tools_cfg.get('searchMode', settings.get('searchMode', 'multi')),
            'fetchEnabled': True,
            'projectPath': tools_cfg.get('projectPath', settings.get('projectPath', '')),
            'codeExecEnabled': tools_cfg.get('codeExecEnabled', settings.get('codeExecEnabled', False)),
            'browserEnabled': tools_cfg.get('browserEnabled', settings.get('browserEnabled', False)),
            'skillsEnabled': tools_cfg.get('skillsEnabled', settings.get('skillsEnabled', True)),
            'swarmEnabled': tools_cfg.get('swarmEnabled', settings.get('swarmEnabled', False)),
            'imageGenEnabled': tools_cfg.get('imageGenEnabled', settings.get('imageGenEnabled', False)),
            'schedulerEnabled': True,
        }

        # 6. Create and start the agentic task
        agentic_task = create_agentic_task(conv_id, messages, config)
        agentic_task_id = agentic_task['id']

        settings['activeTaskId'] = agentic_task_id
        settings_json = json.dumps(settings, ensure_ascii=False)
        db_execute_with_retry(db,
            'UPDATE conversations SET settings=? WHERE id=? AND user_id=1',
            (settings_json, conv_id)
        )

        logger.info('[Timer:%s] Created agentic task %s in conv=%s',
                     timer_id, agentic_task_id[:8], conv_id[:12])

        # 7. Mark timer as triggered
        from lib.database import DOMAIN_SYSTEM
        from lib.database import get_thread_db as _get_db
        sysdb = _get_db(DOMAIN_SYSTEM)
        now_iso = datetime.now().isoformat()
        sysdb.execute(
            "UPDATE timer_watchers SET status='triggered', triggered_at=?, execution_task_id=?, updated_at=? WHERE id=?",
            [now_iso, agentic_task_id, now_iso, timer_id]
        )
        sysdb.commit()

        # 8. Run in background thread
        def _run():
            try:
                run_task(agentic_task)
            except Exception as e:
                logger.error('[Timer:%s] Agentic task %s execution failed: %s',
                             timer_id, agentic_task_id[:8], e, exc_info=True)

        _threading.Thread(target=_run, daemon=True,
                          name=f'timer-exec-{agentic_task_id[:8]}').start()

        # Remove from active timers registry and clean up caches
        with _timers_lock:
            _active_timers.pop(timer_id, None)
        with _cmd_outputs_lock:
            _last_cmd_outputs.pop(timer_id, None)

        return agentic_task_id

    except Exception as e:
        logger.error('[Timer:%s] Failed to execute continuation: %s', timer_id, e, exc_info=True)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Background poll loop
# ═════════════════════════════════════════════════════════════════════════════

def start_timer_loop(timer_id: str) -> None:
    """Start a background daemon thread that polls the timer at its interval.

    The thread self-terminates after:
      - Conditions are met and continuation is executed, OR
      - max_polls is exhausted, OR
      - Timer is cancelled.
    """
    timer = _get_timer_row(timer_id)
    if not timer:
        logger.error('[Timer:%s] Cannot start loop — timer not found', timer_id)
        return

    def _loop():
        tid = timer_id
        logger.info('[Timer:%s] Poll loop started (interval=%ds, max_polls=%d)',
                     tid, timer['poll_interval'], timer['max_polls'])
        poll_interval = timer['poll_interval']
        max_polls = timer['max_polls']

        while True:
            # Check if still active
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry — stopping', tid)
                    break

            # Sleep first (give the initial task time to finish before first poll)
            time.sleep(poll_interval)

            # Re-check after sleep
            with _timers_lock:
                if tid not in _active_timers:
                    logger.info('[Timer:%s] Removed from active registry after sleep — stopping', tid)
                    break

            # Refresh timer state from DB (in case of external cancel)
            current = _get_timer_row(tid)
            if not current or current['status'] != 'active':
                logger.info('[Timer:%s] Status is %s — stopping poll loop',
                            tid, current['status'] if current else 'deleted')
                break

            # Check max_polls
            poll_count = current.get('poll_count', 0)
            if max_polls > 0 and poll_count >= max_polls:
                logger.info('[Timer:%s] Max polls (%d) exhausted — marking exhausted',
                            tid, max_polls)
                _mark_exhausted(tid)
                break

            # Run poll
            try:
                ready, reason, tokens_used, skipped = poll_timer(tid)
            except Exception as e:
                logger.error('[Timer:%s] Poll error: %s', tid, e, exc_info=True)
                _record_poll(tid, 'error', str(e)[:200], 0)
                _increment_poll_count(tid, 'error', str(e)[:200])
                continue

            # Skipped polls (unchanged command output) — no LLM call,
            # no DB record, no SSE event — just silently wait.
            if skipped:
                logger.debug('[Timer:%s] Poll #%d skipped (output unchanged)',
                             tid, poll_count + 1)
                continue

            decision = 'ready' if ready else 'wait'
            _record_poll(tid, decision, reason, tokens_used)
            _increment_poll_count(tid, decision, reason)

            logger.info('[Timer:%s] Poll #%d: %s — %s (tokens=%d)',
                        tid, poll_count + 1, decision, reason[:80], tokens_used)

            if ready:
                logger.info('[Timer:%s] ✅ Conditions met — executing continuation', tid)
                exec_id = _execute_continuation(current)
                if exec_id:
                    logger.info('[Timer:%s] 🚀 Continuation started: task=%s', tid, exec_id[:8])
                else:
                    logger.error('[Timer:%s] ❌ Continuation execution failed', tid)
                break

        logger.info('[Timer:%s] Poll loop ended', tid)
        # Clean up registry
        with _timers_lock:
            _active_timers.pop(tid, None)

    # Register and start
    t = threading.Thread(target=_loop, daemon=True, name=f'timer-poll-{timer_id}')
    with _timers_lock:
        _active_timers[timer_id] = t
    t.start()
    logger.info('[Timer:%s] Background poll thread started', timer_id)


def _increment_poll_count(timer_id: str, decision: str, reason: str) -> None:
    """Update the timer's poll count and last-poll fields in DB."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        now = datetime.now().isoformat()
        db.execute(
            '''UPDATE timer_watchers
               SET poll_count=poll_count+1, last_poll_at=?, last_poll_decision=?,
                   last_poll_reason=?, updated_at=?
               WHERE id=?''',
            [now, decision, reason[:500], now, timer_id]
        )
        db.commit()
    except Exception as e:
        logger.warning('[Timer:%s] Failed to increment poll count: %s', timer_id, e, exc_info=True)


def _mark_exhausted(timer_id: str) -> None:
    """Mark a timer as exhausted (max_polls reached)."""
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        now = datetime.now().isoformat()
        db.execute(
            "UPDATE timer_watchers SET status='exhausted', updated_at=? WHERE id=?",
            [now, timer_id]
        )
        db.commit()
    except Exception as e:
        logger.warning('[Timer:%s] Failed to mark exhausted: %s', timer_id, e, exc_info=True)
    with _timers_lock:
        _active_timers.pop(timer_id, None)
    with _cmd_outputs_lock:
        _last_cmd_outputs.pop(timer_id, None)


# ═════════════════════════════════════════════════════════════════════════════
#  Resume on server restart
# ═════════════════════════════════════════════════════════════════════════════

def resume_active_timers() -> int:
    """Resume all timers with status='active' from DB.

    Called on server startup. Returns the number of timers resumed.
    """
    try:
        from lib.database import DOMAIN_SYSTEM, get_thread_db
        db = get_thread_db(DOMAIN_SYSTEM)
        rows = db.execute(
            "SELECT id FROM timer_watchers WHERE status='active'"
        ).fetchall()

        count = 0
        for row in rows:
            timer_id = row['id']
            with _timers_lock:
                if timer_id not in _active_timers:
                    start_timer_loop(timer_id)
                    count += 1
                    logger.info('[Timer:%s] Resumed on server startup', timer_id)

        if count > 0:
            logger.info('[Timer] Resumed %d active timer(s) on startup', count)
        return count
    except Exception as e:
        logger.warning('[Timer] Failed to resume active timers: %s', e, exc_info=True)
        return 0


def get_active_timer_count() -> int:
    """Return count of in-memory active timer threads."""
    with _timers_lock:
        return len(_active_timers)


__all__ = [
    'create_timer', 'cancel_timer', 'force_trigger_timer',
    'get_timer', 'list_active_timers', 'get_timer_poll_log',
    'poll_timer', 'start_timer_loop', 'resume_active_timers',
    'get_active_timer_count',
    # Used by scheduler/executor.py for inline blocking poll:
    '_record_poll', '_increment_poll_count', '_mark_exhausted',
]
