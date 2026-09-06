"""URL prefetch injection — append fetched content as a tail user carrier.

Extracted from ``orchestrator.py`` (see the package ``__init__`` for the
facade). Isolates :func:`inject_prefetched_urls`, which mutates the
``messages`` list before the main LLM tool loop begins.
"""

from lib.log import get_logger

logger = get_logger(__name__)


def inject_prefetched_urls(messages, prefetched, task):
    """Append auto-fetched URL content as an independent user carrier.

    For each ``(url, content)`` pair in *prefetched*, builds a labelled
    text block (distinguishing PDF vs Web Page) and appends the combined
    block at the message tail without editing the human-authored request.

    Parameters
    ----------
    messages : list[dict]
        Conversation message list — mutated in-place.
    prefetched : list[tuple[str, str]]
        List of ``(url, fetched_content)`` pairs from ``_prefetch_user_urls``.
    task : dict
        Live task dict (used to read ``task['toolRounds']`` count).

    Returns
    -------
    int
        Updated ``tool_round_num`` based on how many tool rounds already
        exist after prefetch.
    """
    if not prefetched:
        return len(task.get('toolRounds', []))

    url_blocks = []
    for url, content in prefetched:
        is_pdf = url.lower().rstrip('/').endswith('.pdf') or content.startswith('[Page ')
        label = 'PDF Document' if is_pdf else 'Web Page'
        url_blocks.append(
            f"=== {label}: {url} ===\n({len(content):,} characters)\n\n{content}"
        )
    urls_text = '\n\n' + ('═' * 40 + '\n\n').join(url_blocks)

    _spliced = '\n\n[Auto-fetched URL content:]\n' + urls_text
    messages.append({
        'role': 'user',
        'content': _spliced,
        '_isMeta': True,
        '_isUrlPrefetch': True,
    })

    try:
        _cid = (task.get('convId') or '') if isinstance(task, dict) else ''
        logger.debug('[Context] conv=%s inject block=prefetched_urls urls=%d chars=%d',
                     (_cid or '?')[:8], len(prefetched), len(_spliced))
    except Exception as _e:
        logger.debug('[Context] prefetched_urls trace failed: %s', _e)

    return len(task.get('toolRounds', []))
