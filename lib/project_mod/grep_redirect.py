"""Runtime shell rewrite for filesystem grep segments in ``run_command``.

No search happens while this module plans a command. A filesystem-reading
``grep`` segment is spliced IN PLACE: everything outside the grep argv —
earlier writes, cd, substitutions, redirections, pipelines, control
operators — stays byte-for-byte where the model put it. Consequently
``write file; grep file`` observes the newly-written bytes, and downstream
consumers such as ``head`` apply real shell backpressure.

Two exec-side splices, both spawn-free (2026-09 redesign; the python helper
shim is retired):

  1. ``rg`` translation (``grep_rg_translate.translate_grep_to_rg``): the
     whole grep argv is replaced by a proven-equivalent ripgrep argv
     (``RIPGREP_CONFIG_PATH=/dev/null`` neutralized). This is the 5x+ FUSE
     fast path grep_search sells, now covering regex patterns, single
     files, context, counts-excluded, and most GNU flags — not just
     literal recursive scans.
  2. GNU fallback: only the program word is replaced with
     ``<prog> --exclude-dir=... [-I]`` — the same descent guards the
     retired helper injected — so arbitrary GNU options keep their native
     stdout, stderr, and exit-code behaviour with zero translation risk.

When the caller supplies no authoritative timeout, the splice is wrapped in
the host-probed coreutils ``timeout`` (exit 124 on expiry, partial output
already streamed), replacing the helper's internal deadline.
``TOFU_GREP_RG=0`` forces the GNU splice; ``TOFU_GREP_FORCE_GNU=1`` and
``TOFU_GREP_EXECUTABLE`` are honoured inside the translator.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass

from lib.log import get_logger
from lib.project_mod.command_analysis import (
    _REDIR_BARE_RE,
    _REDIR_FUSED_RE,
    _grep_segment_reads_filesystem,
    _shell_words,
    _split_pipeline_spans,
)
from lib.project_mod import grep_rg_translate
from lib.project_mod.config import IGNORE_DIRS

logger = get_logger(__name__)

_GREP_BINARIES = frozenset({'grep', 'egrep', 'fgrep'})
_PREFIX_KEYWORDS = frozenset({'if', 'while', 'until', '!', 'time'})
_ENV_ASSIGN_RE = __import__('re').compile(r'^[A-Za-z_][A-Za-z0-9_]*=')

_TIMEOUT_ARGS_CACHE: dict[str, tuple] = {}


def _timeout_binary_args(binary):
    """Probe one timeout binary's advertised options (process-local cache).

    Mirrors the scanner-segment probe in run_command; kept local so this
    module stays import-light (run_command imports us, not the reverse).
    """
    cached = _TIMEOUT_ARGS_CACHE.get(binary)
    if cached is not None:
        return cached
    try:
        probe = subprocess.run(
            [binary, '--help'], capture_output=True, text=True,
            timeout=2, check=False)
        help_text = f'{probe.stdout}\n{probe.stderr}'
    except (OSError, subprocess.SubprocessError):
        help_text = ''
    import re as _re
    args = []
    if '--verbose' in help_text:
        args.append('--verbose')
    if '--signal' in help_text:
        args.append('--signal=TERM')
    elif _re.search(r'(?m)(?:^|[\s,])-s(?:[\s,=]|$)', help_text):
        args.extend(('-s', 'TERM'))
    if '--kill-after' in help_text:
        args.append('--kill-after=2s')
    elif _re.search(r'(?m)(?:^|[\s,])-k(?:[\s,=]|$)', help_text):
        args.extend(('-k', '2s'))
    result = tuple(args)
    if len(_TIMEOUT_ARGS_CACHE) >= 4:
        _TIMEOUT_ARGS_CACHE.clear()
    _TIMEOUT_ARGS_CACHE[binary] = result
    return result


def _default_timeout_wrap():
    """Env-driven segment deadline for direct (non-run_command) planners."""
    try:
        seconds = float(os.environ.get('TOFU_GREP_REDIRECT_DEADLINE_S', '40'))
    except (TypeError, ValueError):
        seconds = 40.0
    if seconds <= 0:
        return None
    binary = shutil.which('timeout')
    if not binary:
        return None
    return (binary, _timeout_binary_args(binary), int(seconds))

# Short flags that give grep an explicit binary policy; when present the
# GNU fallback does not need to inject -I for recursion.
_BINARY_POLICY_CHARS = frozenset('aI')


@dataclass
class GrepRedirectPlan:
    rewritten: object = None
    refused_segment: object = None
    refusal_reason: object = None
    n_redirected: int = 0
    backend: str = ''            # 'rg' | 'gnu' | '' — observability only
    elapsed: float = 0.0


def _argv_end(words, start, segment):
    """Index just past the grep argv, before the first shell redirection."""
    for idx in range(start, len(words)):
        word = words[idx]
        raw = segment[word.start:word.end]
        if not word.squote and not word.dquote and (
                _REDIR_FUSED_RE.match(raw) or _REDIR_BARE_RE.match(raw)):
            return idx
    return len(words)


def _has_binary_policy(words):
    for word in words:
        text = word.text
        if text.startswith('--'):
            if text.split('=', 1)[0] in ('--text', '--binary-files', '--binary'):
                return True
        elif text.startswith('-') and text != '-':
            if any(ch in _BINARY_POLICY_CHARS for ch in text[1:]):
                return True
    return False


def _gnu_splice_replacement(program_text, timeout_prefix):
    """``<prog> --exclude-dir=...`` with an optional timeout wrap.

    Only the descent guards the retired helper injected (``--exclude-dir``);
    binary notices (``Binary file matches``) keep native GNU behavior here,
    exactly as the helper did. The separate hardening layer owns ``-I`` for
    non-spliced recursive greps. ``TOFU_GREP_EXECUTABLE`` substitutes the
    binary, mirroring the helper's ``_which_argv`` override hook.
    """
    parts = []
    if timeout_prefix:
        parts.extend(timeout_prefix)
    override = os.environ.get('TOFU_GREP_EXECUTABLE', '').strip()
    if override and program_text.split('/')[-1] in _GREP_BINARIES:
        parts.append(shlex.quote(override))
    else:
        parts.append(program_text)
    for dirname in sorted(IGNORE_DIRS):
        parts.extend(('--exclude-dir', shlex.quote(dirname)))
    return ' '.join(parts)


def _rg_splice_replacement(rg_argv, timeout_prefix):
    parts = ['RIPGREP_CONFIG_PATH=/dev/null']
    if timeout_prefix:
        parts.extend(timeout_prefix)
    parts.extend(shlex.quote(arg) for arg in rg_argv)
    return ' '.join(parts)


def plan_grep_redirect(command, cwd, timeout_wrap=None):
    """Rewrite filesystem grep segments to rg (preferred) or hardened GNU.

    ``timeout_wrap`` controls the segment deadline splice:
      * ``None`` (default) — compute from ``TOFU_GREP_REDIRECT_DEADLINE_S``
        (40s) against the host-probed coreutils ``timeout``;
      * ``False`` — no wrap (the caller's own timeout is authoritative);
      * ``(binary, args, seconds)`` — use exactly this wrap.

    Returns ``None`` when the command has no direct filesystem grep.
    """
    if os.name != 'posix' or not command or not command.strip():
        return None
    started = time.monotonic()
    if timeout_wrap is None:
        timeout_wrap = _default_timeout_wrap()
    timeout_prefix = None
    if timeout_wrap:
        binary, args, seconds = timeout_wrap
        timeout_prefix = [shlex.quote(binary)]
        timeout_prefix.extend(shlex.quote(a) for a in args)
        timeout_prefix.append(str(int(seconds)))

    replacements = []
    found_any = False
    used_rg = False
    use_rg = os.environ.get('TOFU_GREP_RG', '1') != '0'

    for seg_start, seg_end in _split_pipeline_spans(command):
        segment = command[seg_start:seg_end]
        words = _shell_words(segment)
        if not words:
            continue
        # Keep shell keywords and environment assignments in place. Splicing
        # only the executable+argv works in ``if grep``, ``LC_ALL=C grep``,
        # command substitutions, and subshells without special group syntax.
        idx = 0
        while idx < len(words) and not words[idx].squote and not words[idx].dquote:
            text = words[idx].text
            if _ENV_ASSIGN_RE.match(text) or text in _PREFIX_KEYWORDS:
                idx += 1
                continue
            break
        if idx >= len(words):
            continue
        base = words[idx].text.split('/')[-1]
        if base in ('sudo', 'doas', 'builtin'):
            # Running a substituted binary with elevated authority or through
            # the shell's builtin-only resolver is not a transparent rewrite.
            if any(w.text.split('/')[-1] in _GREP_BINARIES
                   for w in words[idx + 1:]):
                return GrepRedirectPlan(
                    refused_segment=segment.strip(),
                    refusal_reason=f'{base}-wrapped grep cannot be redirected')
            continue
        if base in ('command', 'exec') and idx + 1 < len(words):
            idx += 1
            base = words[idx].text.split('/')[-1]
        if base not in _GREP_BINARIES:
            continue
        argv_end = _argv_end(words, idx, segment)
        grep_words = words[idx:argv_end]
        if any(w.text.startswith('<<') for w in grep_words):
            continue
        if not _grep_segment_reads_filesystem(grep_words):
            continue

        found_any = True
        program_word = words[idx]
        argv_texts = [w.text for w in grep_words[1:]]
        absolute_start = seg_start + program_word.start
        # When a redirection truncates our argv view, operands may live in
        # the untouched tail; rg would read stdin without them, so only the
        # GNU splice (program-word-only replacement) is safe there.
        tail_may_carry_operands = argv_end < len(words)

        rg_argv = None
        if use_rg and not tail_may_carry_operands:
            try:
                rg_argv = grep_rg_translate.translate_grep_to_rg(
                    program_word.text, argv_texts, cwd, sorted(IGNORE_DIRS))
            except Exception as exc:      # translator fault must never
                logger.debug('[grep_redirect] rg translation failed: %s',  # block the GNU splice
                             exc)
                rg_argv = None
        if rg_argv is not None:
            absolute_end = seg_start + grep_words[-1].end
            replacement = _rg_splice_replacement(rg_argv, timeout_prefix)
            used_rg = True
        else:
            absolute_end = seg_start + program_word.end
            replacement = _gnu_splice_replacement(
                segment[program_word.start:program_word.end],
                timeout_prefix)
        replacements.append((absolute_start, absolute_end, replacement))

    if not found_any:
        return None
    rewritten = command
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    return GrepRedirectPlan(
        rewritten=rewritten,
        n_redirected=len(replacements),
        backend='rg' if used_rg else 'gnu',
        elapsed=time.monotonic() - started,
    )


__all__ = ['GrepRedirectPlan', 'plan_grep_redirect']
