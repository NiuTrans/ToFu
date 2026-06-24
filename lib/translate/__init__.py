"""lib.translate — Translation engine + async runtime.

Decomposed from the original 1590-line ``routes/translate.py``. The
Blueprint and route handlers live in ``routes/translate.py`` and stay
deliberately thin; everything else (prompt building, notranslate block
handling, chunking, deduplication, the LLM/MT retry loop, the async
TaskRuntime layer, DB commit, and PPTX file translation) lives here as
focused submodules.

Public surface (what existing callers in chat.py, message_queue.py, the
api_v1 facade, and the tests import). New code should import from this
package facade rather than from submodules directly — the submodule
layout is implementation detail.
"""

# Constants (chunk thresholds, parallelism, TTL)
from .constants import (
    DEFAULT_USER_ID,
    _TRANSLATE_TASK_TTL,
    _SYNC_TRANSLATE_MAX_CHARS,
)

# Prompt + notranslate block handling
from .prompt import (
    _build_translate_prompt,
    _wrap_for_translation,
    _strip_notranslate_tags,
)
from .notranslate import (
    _extract_notranslate_blocks,
    _reattach_notranslate_blocks,
    _NOTRANSLATE_RE,
    _NOTRANSLATE_ALIAS_RE,
    _NT_PLACEHOLDER_FMT,
    _NT_PLACEHOLDER_RE,
    _NT_PLACEHOLDER_LOOSE_RE,
)

# Dedup
from .dedup import _dedup_repetition_loop, _dedup_inline_loop

# Engine (one-chunk LLM/MT dispatcher + retry loop, + chunked free-text wrapper)
from .engine import _translate_one_chunk, _translate_freetext

# Status formatter (used by routes/chat.py auto-translate flow)
from .status import _format_status_message

# Async runtime (TaskRuntime + worker functions)
from .runtime import (
    _translate_runtime,
    _translate_tasks,
    _translate_tasks_lock,
    _cleanup_translate_tasks,
    _do_translate,
)

# Commit (write translated content into the conversations table)
from .commit import (
    _commit_translation_to_db,
    _commit_translation_inner,
    _get_commit_lock,
)

# Incremental per-round translation (agent assistant replies)
from .incremental import (
    submit_round_segment,
    finalize_incremental,
)

# PPTX file translation
from .pptx import (
    _do_translate_pptx,
    _ensure_pptx_upload_dir,
    _PPTX_UPLOAD_DIR,
    _MAX_PPTX_BYTES,
)

__all__ = [
    # constants
    'DEFAULT_USER_ID', '_TRANSLATE_TASK_TTL',
    '_SYNC_TRANSLATE_MAX_CHARS',
    # prompt + notranslate
    '_build_translate_prompt', '_wrap_for_translation', '_strip_notranslate_tags',
    '_extract_notranslate_blocks', '_reattach_notranslate_blocks',
    '_NOTRANSLATE_RE', '_NOTRANSLATE_ALIAS_RE',
    '_NT_PLACEHOLDER_FMT', '_NT_PLACEHOLDER_RE', '_NT_PLACEHOLDER_LOOSE_RE',
    # dedup
    '_dedup_repetition_loop', '_dedup_inline_loop',
    # engine
    '_translate_one_chunk', '_translate_freetext',
    # status
    '_format_status_message',
    # runtime
    '_translate_runtime', '_translate_tasks', '_translate_tasks_lock',
    '_cleanup_translate_tasks', '_do_translate',
    # commit
    '_commit_translation_to_db', '_commit_translation_inner', '_get_commit_lock',
    # incremental per-round translation
    'submit_round_segment', 'finalize_incremental',
    # pptx
    '_do_translate_pptx', '_ensure_pptx_upload_dir',
    '_PPTX_UPLOAD_DIR', '_MAX_PPTX_BYTES',
]
