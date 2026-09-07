"""Environment + current-date sections (ports computeSimpleEnvInfo).

These are the dynamic, per-request fragments — they read ``os.environ`` /
``platform`` / the wall clock. Everything else stays in ``_sections``.
"""
from __future__ import annotations

import os
import platform
from datetime import datetime, timezone

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
#  Section 10 — # Environment  (ports computeSimpleEnvInfo)
# ═══════════════════════════════════════════════════════════════════════════════

def _short_os_version() -> str:
    """Return a short OS version string — strip vendor/build suffixes.

    ``platform.release()`` on the host returns strings like
    ``4.18.0-147.mt20200626.413.el8_1.x86_64``, which leak the vendor
    build identifier. We keep the major.minor (everything before the
    first ``-``) plus the system name.
    """
    try:
        sysname = platform.system()
        rel = platform.release() or ''
        # On Linux, take everything before the first hyphen ("4.18.0").
        # On macOS / Windows the release string is already short.
        short = rel.split('-', 1)[0] if sysname == 'Linux' else rel
        return f"{sysname} {short}".strip()
    except Exception as e:
        logger.debug('[SysPrompt] platform lookup failed: %s', e)
        return "unknown"


def section_environment(cwd: str, is_git: bool, model: str,
                         extra_roots: list[str] | None = None,
                         has_real_tools: bool = True) -> str:
    """Port of Claude Code's computeSimpleEnvInfo.

    Args:
        cwd:            Primary working directory. When empty, the bullet
                        is dropped (project mode off).
        is_git:         Whether ``cwd`` is inside a git repository.
        model:          Ignored — Tofu has too many internal aliases
                        for the "powered by model X" bullet to be
                        consistently truthful (Claude vs OpenAI vs
                        Meituan, all routed through the same pipeline).
                        Kept in the signature for caller back-compat.
        extra_roots:    Multi-root workspace extras, or None.
        has_real_tools: When False, drop ``Shell`` and ``OS Version`` —
                        they only matter for ``run_command``.
    """
    shell = os.environ.get('SHELL', '') or ''
    if 'zsh' in shell:
        shell_name = 'zsh'
    elif 'bash' in shell:
        shell_name = 'bash'
    else:
        shell_name = shell or 'unknown'

    # Primary working directory takes top billing; then the git flag, then
    # additional roots, then platform.  Order matches Claude Code verbatim.
    # When cwd is empty (project mode off), drop the bullet entirely.
    bullets: list[str] = []
    if cwd:
        bullets.append(f" - Primary working directory: {cwd}")
        bullets.append(f"   - Is a git repository: {'true' if is_git else 'false'}")

    if extra_roots:
        bullets.append(" - Additional working directories:")
        for r in extra_roots:
            bullets.append(f"   - {r}")

    import sys as _sys
    bullets.append(f" - Platform: {_sys.platform}")
    if has_real_tools:
        # Shell + kernel only matter for the run_command tool. Without
        # tools the model has no shell access — these bullets are dead
        # weight (and the kernel string used to leak vendor build IDs).
        bullets.append(f" - Shell: {shell_name}")
        bullets.append(f" - OS Version: {_short_os_version()}")

    return (
        "# Environment\n"
        "You have been invoked in the following environment: \n"
        + "\n".join(bullets)
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  Section 11 — Current date (cache-stable, changes once per UTC day)
# ═══════════════════════════════════════════════════════════════════════════════

def section_current_date() -> str:
    return f"Current date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
