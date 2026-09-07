# HOT_PATH
"""Code execution handler: run_command (shell commands in project sandbox)."""

from __future__ import annotations

import re
import threading
import time

from lib.log import get_logger
from lib.project_mod.config import MAX_COMMAND_OUTPUT
from lib.tasks_pkg.executor import _finalize_tool_round, tool_registry
from lib.tasks_pkg.manager import append_event

logger = get_logger(__name__)

try:
    from lib.qr import LiveQrScanner as _LiveQrScanner
except Exception as _e:  # pragma: no cover - defensive: never break run_command
    _LiveQrScanner = None
    logger.warning('[code_exec] live QR scanning unavailable: %s', _e)


# ── Streaming output coalescing ─────────────────────────────
# The subprocess can emit chunks faster than the SSE channel can deliver them
# (especially over VSCode port-forward / nginx — see the SSE proxy buffering
# memory).  Coalesce chunks into ≤ COALESCE_BYTES OR flush every COALESCE_MS,
# whichever first.  Tunable knobs (per CLAUDE.md §10.1, hyperparameters need
# user approval — defaults agreed in chat).
_COALESCE_MS = 200            # max wall-clock between flushes
_COALESCE_BYTES = 4096        # flush as soon as buffered output exceeds this
# Keep very short commands on the terminal-result path only. Persisting a
# running-round checkpoint is essential for long commands, but doing it before
# every ``pwd``/``git status``/``echo`` makes storage latency part of command
# latency. Commands that outlive this grace period retain the existing live
# output + reconnect durability contract.
RUN_COMMAND_LIVE_GRACE_MS = 350
RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS = max(1, int(MAX_COMMAND_OUTPUT))


class _RunCommandSpawnLifecycle:
    """Delay presentation/durability until a command proves it is long-lived.

    The subprocess callback itself only stamps the authoritative clocks and
    arms a timer. The timer publishes the running frame and checkpoint from a
    background thread, keeping event-store latency out of the subprocess start
    path. ``finish`` cancels that work for commands that settle inside the
    grace window.
    """

    def __init__(self, task, rn, round_entry, grace_ms):
        self._task = task
        self._rn = rn
        self._round_entry = round_entry
        self._grace_s = max(0.0, float(grace_ms) / 1000.0)
        self._condition = threading.Condition()
        self._started = False
        self._finished = False
        self._publishing = False
        self._ready = False
        self._timer = None
        self._exec_start_ms = None
        self._deadline_ms = None
        self._live_callbacks = []

    def __call__(self, exec_start_ms, deadline_ms):
        with self._condition:
            if self._started:
                return
            self._started = True
            self._exec_start_ms = exec_start_ms
            self._deadline_ms = deadline_ms
            self._round_entry['execStartTs'] = exec_start_ms
            if deadline_ms is not None:
                self._round_entry['deadlineTs'] = deadline_ms
            else:
                self._round_entry.pop('deadlineTs', None)
            if self._finished:
                return
            timer = threading.Timer(self._grace_s, self._start_publish)
            timer.daemon = True
            self._timer = timer
            timer.start()

    def add_live_callback(self, callback):
        call_now = False
        with self._condition:
            if self._ready:
                call_now = True
            elif not self._finished:
                self._live_callbacks.append(callback)
        if call_now:
            callback()

    @property
    def is_live(self):
        with self._condition:
            return self._ready

    def ensure_live(self):
        """Promote a chatty command before the time grace expires."""
        self._start_publish(background=True)

    def _start_publish(self, background=False):
        with self._condition:
            if (not self._started or self._finished or self._publishing
                    or self._ready):
                return
            self._publishing = True
            timer = self._timer
            self._timer = None
            if timer is not None and timer is not threading.current_thread():
                timer.cancel()
        if background:
            worker = threading.Thread(
                target=self._publish,
                name='tofu-run-command-live-start',
                daemon=True,
            )
            worker.start()
        else:
            self._publish()

    def _publish(self):
        ev = {
            'type': 'tool_progress',
            'roundNum': self._rn,
            'toolCallId': self._round_entry.get('toolCallId', ''),
            'toolName': self._round_entry.get('toolName') or 'run_command',
            'stream': 'stdout',
            'chunk': '',
            'execStartTs': self._exec_start_ms,
        }
        if self._deadline_ms is not None:
            ev['deadlineTs'] = self._deadline_ms
        try:
            append_event(self._task, ev)
        except Exception as exc:
            logger.warning(
                '[code_exec] live-start event failed (non-fatal) task=%s '
                'round=%s: %s',
                (self._task.get('id') or '?')[:8], self._rn, exc)

        # Output may only overtake the empty live-start frame after that frame
        # has passed through append_event, preserving presentation order.
        with self._condition:
            self._ready = True
            callbacks = self._live_callbacks
            self._live_callbacks = []
        for callback in callbacks:
            try:
                callback()
            except Exception as exc:
                logger.warning(
                    '[code_exec] live-start callback failed (non-fatal): %s',
                    exc)

        if not self._task.get('_suppressCheckpoint'):
            try:
                from lib.tasks_pkg.manager import checkpoint_task_partial
                checkpoint_task_partial(self._task, force=True)
            except Exception as exc:
                logger.warning(
                    '[code_exec] spawn checkpoint failed (non-fatal) '
                    'task=%s round=%s: %s',
                    (self._task.get('id') or '?')[:8], self._rn, exc)
        with self._condition:
            self._publishing = False
            self._condition.notify_all()

    def finish(self):
        """Settle the lifecycle and return whether live state was published."""
        with self._condition:
            self._finished = True
            timer = self._timer
            self._timer = None
            if timer is not None:
                timer.cancel()
            # A promoted command must finish its running checkpoint before the
            # terminal round can be emitted; otherwise an older running
            # snapshot could race and overwrite the terminal state.
            while self._publishing:
                self._condition.wait()
            return self._ready


def _defer_run_command_progress(callback, lifecycle):
    """Buffer small early output until the command enters its live phase."""
    if lifecycle is None:
        return callback

    state = {
        'lock': threading.Lock(),
        'active': False,
        'detached': False,
        'buffer': [],
        'bytes': 0,
    }

    def _activate():
        with state['lock']:
            if state['detached']:
                state['buffer'] = []
                state['bytes'] = 0
                return
            state['active'] = True
            buffered = state['buffer']
            state['buffer'] = []
            state['bytes'] = 0
        for stream, text in buffered:
            callback(stream, text)

    lifecycle.add_live_callback(_activate)

    def _on_chunk(stream, text):
        if not text:
            return
        activate = False
        with state['lock']:
            if state['detached']:
                return
            if state['active']:
                direct = True
            else:
                direct = False
                state['buffer'].append((stream, text))
                state['bytes'] += len(text.encode('utf-8'))
                activate = state['bytes'] >= _COALESCE_BYTES
        if direct:
            callback(stream, text)
        elif activate:
            lifecycle.ensure_live()

    def _flush():
        with state['lock']:
            active = state['active']
            if not active or state['detached']:
                # The final authoritative tool result already contains this
                # small output; an intermediate progress write adds no value.
                state['buffer'] = []
                state['bytes'] = 0
        if active and not state['detached']:
            callback.flush()

    _on_chunk.flush = _flush

    close = getattr(callback, 'close', None)
    if callable(close):
        def _close(terminal_reason=None):
            with state['lock']:
                active = state['active']
                if not active:
                    state['buffer'] = []
                    state['bytes'] = 0
            return close(terminal_reason if active else None)
        _on_chunk.close = _close

    finalize_output = getattr(callback, 'finalize_output', None)
    if callable(finalize_output):
        def _finalize(*, complete=True):
            with state['lock']:
                active = state['active']
                if not active:
                    state['buffer'] = []
                    state['bytes'] = 0
            if active:
                return finalize_output(complete=complete)
            # Close without a terminal progress frame. The writer has not
            # received data on this path, but finalizing it preserves the
            # artifact API expected by the shared handler.
            if callable(close):
                close()
            return callback.output_writer.finalize(complete=complete)
        _on_chunk.finalize_output = _finalize

    if hasattr(callback, 'output_writer'):
        _on_chunk.output_writer = callback.output_writer

    def _detach():
        """Stop foreground presentation while the subprocess keeps draining."""
        with state['lock']:
            state['detached'] = True
            state['buffer'] = []
            state['bytes'] = 0

    _on_chunk.detach = _detach
    return _on_chunk


def _remember_bounded_partial_output(state, text):
    """Retain one command-output prefix/tail inside the recovery budget."""
    if not text:
        return
    state['partial_total_chars'] += len(text)
    prefix = state['partial_prefix']
    prefix_room = state['partial_prefix_limit'] - len(prefix)
    if prefix_room > 0:
        prefix += text[:prefix_room]
        text = text[prefix_room:]
        state['partial_prefix'] = prefix
    if text:
        tail = state['partial_suffix'] + text
        state['partial_suffix'] = tail[-state['partial_suffix_limit']:]


def _render_bounded_partial_output(state):
    """Render exact output below the cap, otherwise a bounded prefix/tail."""
    total = state['partial_total_chars']
    prefix = state['partial_prefix']
    suffix = state['partial_suffix']
    limit = RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS
    if total <= limit:
        return prefix + suffix
    marker = f'\n\n… [live output truncated: {total:,} chars total] …\n\n'
    if len(marker) >= limit:
        return marker[:limit]
    available = limit - len(marker)
    prefix_size = min(len(prefix), available * 3 // 4)
    suffix_size = available - prefix_size
    tail = suffix[-suffix_size:] if suffix_size else ''
    return prefix[:prefix_size] + marker + tail


def _make_run_command_progress_cb(
        task, rn, round_entry, command, runtime_context=None, lifecycle=None):
    """Build an ``on_chunk(stream, text)`` callback for tool_run_command.

    Each call appends the chunk to ``round_entry['_partialOutput']`` for
    state-snapshot recovery, and emits a coalesced ``tool_progress`` SSE
    event so the frontend can render output as it arrives.

    Coalescing: chunks are buffered for up to ``_COALESCE_MS`` or
    ``_COALESCE_BYTES`` (whichever comes first) before being flushed as a
    single SSE event.  This avoids flooding the event queue when a command
    produces tight-loop output (e.g. ``yes``, build logs).
    """
    if runtime_context is not None:
        from lib.tasks_pkg.tool_runtime import bind_tool_progress_sink

        sink = bind_tool_progress_sink(runtime_context)
        writer = runtime_context.open_output_writer()
        scanner = _LiveQrScanner() if _LiveQrScanner is not None else None

        def _publish(stream, text):
            if not text:
                return
            writer.write(text)
            sink.publish(stream, text)

        def _flush():
            sink.flush()
            if scanner is None:
                return
            try:
                fresh = scanner.scan(sink.snapshot)
            except Exception as exc:
                logger.warning(
                    '[code_exec] live QR scan failed (non-fatal): %s', exc)
                return
            if not fresh:
                return
            acc = list(round_entry.get('qrImages') or [])
            acc.extend(fresh)
            round_entry['qrImages'] = acc
            sink.publish('stdout', '', qrImages=acc)
            sink.flush()

        def _finalize(*, complete=True):
            _flush()
            sink.close('completed' if complete else 'cancelled')
            return writer.finalize(complete=complete)

        _publish.flush = _flush
        _publish.close = sink.close
        _publish.finalize_output = _finalize
        _publish.output_writer = writer
        return _defer_run_command_progress(_publish, lifecycle)

    state = {
        'buf': [],            # list[(stream, text)]
        'bytes': 0,
        'last_flush': time.monotonic(),
        'lock': threading.Lock(),
        'timer': None,
        # A command can print without bound even though its settled result is
        # capped by MAX_COMMAND_OUTPUT. Keep the reconnect projection at the
        # same hard budget instead of repeatedly copying the entire raw log.
        'partial_prefix': '',
        'partial_suffix': '',
        'partial_total_chars': 0,
        'partial_prefix_limit': (
            RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS * 3 // 4),
        'partial_suffix_limit': max(
            1, RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS
            - RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS * 3 // 4),
        # Live QR recovery. A scan-to-login QR is printed while the command
        #   is STILL RUNNING and blocking for the scan, so recovering it only
        #   at finalize delivers the image after the authorization window has
        #   closed. The scanner is stateful (dedup + growth throttle) because
        #   this callback fires every ~200ms for the whole wait.
        'qr': _LiveQrScanner() if _LiveQrScanner is not None else None,
    }

    def _flush_locked():
        if not state['buf']:
            return
        # Merge consecutive same-stream chunks for compactness
        merged = []
        cur_stream = None
        cur_parts = []
        for s, t in state['buf']:
            if s == cur_stream:
                cur_parts.append(t)
            else:
                if cur_stream is not None:
                    merged.append((cur_stream, ''.join(cur_parts)))
                cur_stream = s
                cur_parts = [t]
        if cur_stream is not None:
            merged.append((cur_stream, ''.join(cur_parts)))

        state['buf'] = []
        state['bytes'] = 0
        state['last_flush'] = time.monotonic()
        if state['timer'] is not None:
            try:
                state['timer'].cancel()
            except Exception as e:
                logger.debug('[run_command progress] timer cancel failed: %s', e)
            state['timer'] = None

        # Mirror partial output onto the round_entry so a
        # state-snapshot reconnect can replay it (see manager.append_event).
        for s, t in merged:
            _remember_bounded_partial_output(state, t)
            partial = _render_bounded_partial_output(state)
            round_entry['_partialOutput'] = partial
            round_entry['_partialOutputTotalChars'] = state[
                'partial_total_chars']
            if state['partial_total_chars'] > RUN_COMMAND_RECOVERY_OUTPUT_MAX_CHARS:
                round_entry['_partialOutputTruncated'] = True
            else:
                round_entry.pop('_partialOutputTruncated', None)
            append_event(task, {
                'type': 'tool_progress',
                'roundNum': rn,
                'toolCallId': round_entry.get('toolCallId', ''),
                'stream': s,
                'chunk': t,
                'toolName': round_entry.get('toolName') or 'run_command',
            })

        # Live QR recovery — the scan-to-login seam.
        #   Runs AFTER the buffer is updated so the scanner sees the complete
        #   art block, and emits its own event rather than riding a chunk
        #   frame: the code becomes visible the moment it is drawable, which
        #   is the entire point (the command is still blocking for the scan).
        #   Descriptors are also stamped onto round_entry so a reconnect /
        #   state-snapshot replay restores them without a re-scan.
        scanner = state.get('qr')
        if scanner is not None:
            try:
                fresh = scanner.scan(partial)
            except Exception as e:
                logger.warning('[code_exec] live QR scan failed (non-fatal): %s', e)
                fresh = []
            if fresh:
                acc = list(round_entry.get('qrImages') or [])
                acc.extend(fresh)
                round_entry['qrImages'] = acc
                append_event(task, {
                    'type': 'tool_progress',
                    'roundNum': rn,
                    'toolCallId': round_entry.get('toolCallId', ''),
                    'stream': 'stdout',
                    'chunk': '',
                    'toolName': round_entry.get('toolName') or 'run_command',
                    'qrImages': acc,
                })
                logger.info('[code_exec] surfaced %d live QR code(s) for a '
                            'still-running command (task=%s)',
                            len(fresh), task.get('id', '?')[:8])

    def _delayed_flush():
        with state['lock']:
            _flush_locked()

    def _on_chunk(stream, text):
        if not text:
            return
        with state['lock']:
            state['buf'].append((stream, text))
            state['bytes'] += len(text)
            now = time.monotonic()
            if (state['bytes'] >= _COALESCE_BYTES
                    or (now - state['last_flush']) * 1000 >= _COALESCE_MS):
                _flush_locked()
            elif state['timer'] is None:
                # Schedule a deferred flush so the last partial chunk
                # doesn't sit forever waiting for a follow-up.
                t = threading.Timer(_COALESCE_MS / 1000.0, _delayed_flush)
                t.daemon = True
                state['timer'] = t
                t.start()

    # Expose a final-flush hook so the handler can drain after the command
    # exits (in case the last chunk fell below the threshold).
    def _final_flush():
        with state['lock']:
            _flush_locked()
    _on_chunk.flush = _final_flush  # attribute on closure for the handler
    return _defer_run_command_progress(_on_chunk, lifecycle)


def _make_run_command_spawn_cb(task, rn, round_entry):
    """Build an ``on_spawn(exec_start_ms, deadline_ms)`` callback.

    Fires ONCE when the subprocess is spawned. The authoritative clocks are
    stamped immediately, while their presentation event and running-round
    checkpoint are delayed briefly so short commands only pay for their final
    result.

    1. **Publishes the deadline.** The countdown cannot be derived on the
       client: the effective budget is the requested ``timeout`` AFTER the
       cross-DC multiplier and the ``MAX_COMMAND_TIMEOUT`` clamp, and the
       round's ``tStart`` is the ANNOUNCE time, not the spawn time (a write
       approval can sit minutes in between). Both clocks are therefore
       stamped by the backend and shipped verbatim.

    2. **Forces a checkpoint.** ``deadlineTs`` only exists AFTER the round was
       announced, so it can never ride the ``tool_start`` frame. And during a
       long command NEITHER periodic checkpoint fires — the orchestrator's runs
       after a round completes, the stream's on a content delta — so whether a
       running round reached the DB was a race. Without this write, switching
       conversations mid-command would find no round to project and the
       countdown would restart from nothing, which is exactly the failure this
       feature exists to prevent.

    Best-effort throughout: telemetry must never abort a running command.
    """
    return _RunCommandSpawnLifecycle(
        task, rn, round_entry, RUN_COMMAND_LIVE_GRACE_MS)


def _make_grep_intercept_cb(task, rn, round_entry):
    """Publish display-only metadata when run_command delegates a file grep.

    The empty progress frame updates the running card immediately. No marker is
    appended to ``toolContent`` or subprocess output, so the model protocol and
    restored command transcript remain a normal ``run_command`` invocation.
    """
    fired = {'done': False}

    def _on_intercept(_count=1):
        if fired['done']:
            return
        fired['done'] = True
        round_entry['grepSearchIntercepted'] = True
        append_event(task, {
            'type': 'tool_progress',
            'roundNum': rn,
            'toolCallId': round_entry.get('toolCallId', ''),
            'toolName': round_entry.get('toolName') or 'run_command',
            'stream': 'stdout',
            'chunk': '',
            'grepSearchIntercepted': True,
        })

    return _on_intercept


def _make_stdin_callback(task, rn, round_entry, command):
    """Create a callback that pauses execution and asks the user for stdin input.

    When the subprocess appears to be waiting for stdin (no output for N seconds),
    this callback:
    1. Emits a ``stdin_request`` SSE event with the prompt context
    2. Blocks until the user submits input via ``/api/chat/stdin_response``
    3. Returns the user's input string (or None if aborted)
    """
    from lib.ids import short_id
    from lib.tasks_pkg.stdin_handler import request_stdin

    def _stdin_cb(prompt_hint):
        stdin_id = short_id('stdin_', 12)
        logger.info('[Executor] stdin wait detected for command=%s, '
                    'stdin_id=%s, prompt_hint=%.200s',
                    command[:80], stdin_id, prompt_hint)

        round_entry['status'] = 'awaiting_stdin'
        round_entry['stdinId'] = stdin_id
        round_entry['stdinPrompt'] = prompt_hint
        append_event(task, {
            'type': 'stdin_request',
            'roundNum': rn,
            'toolCallId': round_entry.get('toolCallId', ''),
            'stdinId': stdin_id,
            'prompt': prompt_hint,
            'command': command[:200],
        })

        user_input = request_stdin(stdin_id, task=task)

        if user_input is not None:
            round_entry['status'] = 'searching'
            round_entry.pop('stdinId', None)
            round_entry.pop('stdinPrompt', None)
            append_event(task, {
                'type': 'stdin_resolved',
                'roundNum': rn,
                'toolCallId': round_entry.get('toolCallId', ''),
                'stdinId': stdin_id,
            })

        return user_input

    return _stdin_cb


# code_exec is registered as a special handler (matched via round_entry, not fn_name)
def _project_output_artifact(tool_content, artifact):
    """Use the writer preview while preserving command terminal markers."""
    if artifact is None or not artifact.spilled:
        return tool_content

    prefix_match = re.match(r'^(\$ .*?\n)', tool_content)
    prefix = prefix_match.group(1) if prefix_match else ''
    terminal_match = re.search(
        r'(\n\[Command (?:timed out|aborted by user|interrupted by [^\]]+)\])?'
        r'(\n\[exit code: -?\d+\])\s*$',
        tool_content,
    )
    terminal = terminal_match.group(0).rstrip() if terminal_match else ''
    preview = artifact.text

    if artifact.artifact_ref:
        state = 'complete' if artifact.complete else 'cancellation-partial'
        notice = (
            f'\n[Output overflow: {artifact.size_bytes:,} bytes; {state}; '
            f'artifact_ref={artifact.artifact_ref}]')
    else:
        reason = artifact.degraded_reason or 'storage'
        notice = (
            f'\n[Output overflow was not retained ({reason}); '
            f'{artifact.size_bytes:,} bytes observed]')
    suffix = notice + (f'\n{terminal}' if terminal else '')
    return prefix + preview + suffix


def _register_output_artifact_origin(task, round_entry, artifact, *,
                                     tool_name, display, tool_call_id):
    """Register owner-local provenance for continuation-tool presentation."""
    if artifact is None or not artifact.artifact_ref:
        return
    from lib.tool_result_artifacts import register_artifact_provenance

    register_artifact_provenance(
        task,
        artifact.artifact_ref,
        tool_name=tool_name,
        display=display,
        llm_round=round_entry.get('llmRound'),
        tool_call_id=tool_call_id,
    )


@tool_registry.special('__code_exec__', category='code',
                       description='Execute a shell command in the project sandbox')
def _handle_code_exec(task, tc, fn_name, tc_id, fn_args, rn, round_entry, cfg, project_path, project_enabled, all_tools=None):
    from lib.project_mod import execute_standalone_command
    cmd = fn_args.get('command', '')
    from lib.tasks_pkg.handlers.authenticated_download import (
        maybe_redirect_authenticated_download,
    )
    redirected = maybe_redirect_authenticated_download(
        task=task, cfg=cfg, command=cmd)
    if redirected is not None:
        round_entry['authenticatedDownloadRedirected'] = True
        meta = {
            'toolName': 'code_exec',
            'command': redirected.display_command,
            'output': redirected.tool_content,
            'exitCode': '0' if redirected.ok else 'not-run',
            'timedOut': False,
            'badge': redirected.badge,
            'authenticatedDownloadRedirected': True,
        }
        if redirected.receipt:
            meta['serverStagingReceipt'] = redirected.receipt
        _finalize_tool_round(
            task, rn, round_entry, [meta],
            status='done' if redirected.ok else 'error')
        return tc_id, redirected.tool_content, False
    # Background swarm/agent workers have no UI capable of answering a stdin
    # request. Giving them the interactive callback changes run_command to its
    # stdin-waiting implementation and can strand a worker indefinitely.
    cb = (None if task.get('_unattended')
          else _make_stdin_callback(task, rn, round_entry, cmd))
    from lib.tasks_pkg.tool_runtime import active_context_for_call
    runtime_context = active_context_for_call(
        task, round_num=rn, tool_call_id=tc_id, round_entry=round_entry)
    spawn_cb = _make_run_command_spawn_cb(task, rn, round_entry)
    progress_cb = _make_run_command_progress_cb(
        task, rn, round_entry, cmd, runtime_context=runtime_context,
        lifecycle=spawn_cb)
    grep_intercept_cb = _make_grep_intercept_cb(task, rn, round_entry)
    from lib.tasks_pkg.handlers._background_command import (
        is_background_command_result,
        run_with_steer_handoff,
    )

    def _detach_progress():
        detach = getattr(progress_cb, 'detach', None)
        if callable(detach):
            detach()

    try:
        # task= (): without it the runner got task=None — the
        #   subprocess was NEVER registered (_subprocess_pid), so a silent
        #   >30min code_exec was still whole-task-reaped (the reaper could
        #   not interrupt it) and even Stop could not kill the process
        #   (the aborted poll was dead code under task=None).
        tool_content = run_with_steer_handoff(
            task=task,
            config=cfg,
            command=cmd,
            on_detach=_detach_progress,
            execute=lambda command_task: execute_standalone_command(
                fn_name, fn_args,
                stdin_callback=cb,
                on_chunk=progress_cb,
                on_spawn=spawn_cb,
                on_grep_intercept=grep_intercept_cb,
                runtime_context=runtime_context,
                task=command_task,
            ),
        )
    finally:
        # Cancel the live-start timer for short commands, or join an in-flight
        # running checkpoint before the terminal round is emitted.
        spawn_cb.finish()
        # Flush any buffered tail that didn't reach the coalescing threshold.
        try:
            progress_cb.flush()
        except Exception as e:
            logger.debug('[code_exec] progress flush failed: %s', e)
    artifact = None
    finalize_output = getattr(progress_cb, 'finalize_output', None)
    if callable(finalize_output):
        backgrounded = is_background_command_result(tool_content)
        incomplete = (
            runtime_context is not None and runtime_context.cancellation_requested
        ) or '[Command timed out]' in tool_content or (
            '[Command interrupted by' in tool_content) or backgrounded
        artifact = finalize_output(complete=not incomplete)
        tool_content = _project_output_artifact(tool_content, artifact)
        _register_output_artifact_origin(
            task, round_entry, artifact,
            tool_name='run_command', display=cmd, tool_call_id=tc_id)
        if artifact.spilled:
            round_entry['outputArtifact'] = {
                'artifactRef': artifact.artifact_ref,
                'sizeBytes': artifact.size_bytes,
                'complete': artifact.complete,
                'degraded': artifact.degraded,
                'degradedReason': artifact.degraded_reason,
            }
    # Must anchor to END — command output may itself contain [exit code: N]
    m_exit = re.search(r'\[exit code: (-?\d+)\]\s*$', tool_content)
    exit_code = m_exit.group(1) if m_exit else '?'
    timed_out = '[Command timed out]' in tool_content
    # Per-command interrupt (user button / stall watchdog) — same contract
    #   as lib/tools/meta.py::_build_run_command: an amber neutral stop, the
    #   task CONTINUED, never the red `exit -1` error frame.
    interrupted = '[Command interrupted by' in tool_content
    backgrounded = is_background_command_result(tool_content)
    # No marker + not a timeout = the command was REFUSED/BLOCKED before it
    # ran (read-only root, dangerous pattern, no project path, pre-hook block,
    # abort, or a start error). Classify it as not-run with the full message
    # as the reason so the UI shows a clear cause instead of "exit ?".
    not_run = (m_exit is None) and (not timed_out) and (not backgrounded)
    prefix = f'$ {cmd}\n'
    if tool_content.startswith(prefix):
        output_text = tool_content[len(prefix):]
    else:
        output_lines = tool_content.split('\n', 1)
        output_text = output_lines[1] if len(output_lines) > 1 else ''
    output_text = re.sub(r'\n?\[exit code: -?\d+\]\s*$', '', output_text).strip()
    output_text = re.sub(r'\n?\[Command timed out\].*$', '', output_text).strip()
    output_text = re.sub(r'\n?\[Command interrupted by[^\n]*\].*$', '', output_text).strip()
    if not_run:
        from lib.tools.meta import _classify_not_run_badge
        reason = (tool_content or '').strip()
        logger.warning('[code_exec] command not run (refused/blocked/error) '
                       'cmd=%.80s reason=%.160s', cmd, reason)
        meta = {
            'toolName': 'code_exec', 'command': cmd,
            'output': reason, 'reason': reason,
            'exitCode': 'not-run', 'notRun': True, 'timedOut': False,
            'badge': _classify_not_run_badge(reason),
        }
    elif backgrounded:
        meta = {
            'toolName': 'code_exec', 'command': cmd,
            'output': output_text, 'exitCode': 'background',
            'timedOut': False, 'backgrounded': True,
            'badge': 'background',
        }
    else:
        meta = {
            'toolName': 'code_exec', 'command': cmd, 'output': output_text,
            'exitCode': 'timeout' if timed_out else exit_code, 'timedOut': timed_out,
        }
        if interrupted:
            meta['interrupted'] = True
            meta['badge'] = 'interrupted'
    if round_entry.get('grepSearchIntercepted'):
        meta['grepSearchIntercepted'] = True
    _finalize_tool_round(task, rn, round_entry, [meta])
    return tc_id, tool_content, False
