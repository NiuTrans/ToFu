"""lib/mt_provider/_translate.py — Public MT translate entrypoints.

``mt_translate`` performs a single MT-provider translation with markdown
preservation; ``mt_translate_chunked`` splits long inputs on paragraph/line
boundaries to respect the NiuTrans 5000-char per-request limit.
"""

from lib.log import get_logger

from ._config import _get_mt_config, _normalize_lang
from ._markdown import (
    _extract_code_blocks,
    _restore_code_blocks,
    _extract_md_structure,
    _restore_md_structure,
)
from ._niutrans import _niutrans_translate

logger = get_logger(__name__)


def mt_translate(text, source='', target='zh'):
    """Translate text using the configured machine translation provider.

    Automatically extracts code blocks, markdown structural prefixes, and
    inline markers before translation and reinserts them after, since MT
    APIs don't understand markdown formatting.

    Preservation order:
    1. Fenced code blocks (```...```) and inline code (`...`)
    2. Markdown line prefixes (headings ###, lists - / 1., blockquotes >)
    3. Bold/italic markers (**text**, *text*)

    Args:
        text: Text to translate.
        source: Source language name/code (empty or 'auto' for auto-detect).
        target: Target language name/code.

    Returns:
        Translated text string.

    Raises:
        ValueError: If MT provider is not configured.
        RuntimeError: If the API call fails.
    """
    cfg = _get_mt_config()
    if not cfg:
        raise ValueError('Machine translation provider not configured')

    # Short-circuit: source == target → return as-is
    src_norm = _normalize_lang(source)
    tgt_norm = _normalize_lang(target)
    if src_norm and src_norm != 'auto' and src_norm == tgt_norm:
        return text

    # Step 1: Extract code blocks to protect them from MT corruption
    clean_text, code_blocks = _extract_code_blocks(text)
    if code_blocks:
        logger.debug('[MT] Extracted %d code blocks before translation', len(code_blocks))

    # Step 2: Extract markdown structural prefixes (headings, lists, blockquotes)
    clean_text, md_prefixes = _extract_md_structure(clean_text)

    provider = cfg.get('provider', 'niutrans')
    if provider == 'niutrans':
        translated = _niutrans_translate(clean_text, source, target, cfg)
    else:
        raise ValueError('Unknown MT provider: %s' % provider)

    # Restore in reverse order
    # Step 2r: Restore markdown structural prefixes
    translated = _restore_md_structure(translated, md_prefixes)

    # Step 1r: Restore code blocks
    if code_blocks:
        translated = _restore_code_blocks(translated, code_blocks)

    return translated


def mt_translate_chunked(text, source='', target='zh', max_chunk=4500):
    """Translate text with automatic chunking for long inputs.

    NiuTrans has a 5000 char limit per request. This function splits
    longer texts on paragraph boundaries and translates each chunk.

    Args:
        text: Text to translate.
        source: Source language.
        target: Target language.
        max_chunk: Max chars per chunk (default 4500, leaving margin).

    Returns:
        Translated text.
    """
    if len(text) <= max_chunk:
        return mt_translate(text, source, target)

    # Split on paragraph boundaries
    paragraphs = text.split('\n\n')
    chunks = []
    current = ''
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chunk:
            chunks.append(current.strip())
            current = para
        else:
            current = current + '\n\n' + para if current else para
    if current.strip():
        chunks.append(current.strip())

    # Further split any chunks that are still too long
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chunk:
            final_chunks.append(chunk)
        else:
            # Split on single newlines
            lines = chunk.split('\n')
            cur = ''
            for line in lines:
                if cur and len(cur) + len(line) + 1 > max_chunk:
                    final_chunks.append(cur.strip())
                    cur = line
                else:
                    cur = cur + '\n' + line if cur else line
            if cur.strip():
                final_chunks.append(cur.strip())

    if not final_chunks:
        return mt_translate(text, source, target)

    logger.info('[MT] Chunked translation: %d chars → %d chunks', len(text), len(final_chunks))

    translated_parts = []
    for i, chunk in enumerate(final_chunks):
        try:
            part = mt_translate(chunk, source, target)
            translated_parts.append(part)
        except Exception as e:
            logger.error('[MT] Chunk %d/%d failed: %s', i + 1, len(final_chunks), e)
            raise

    return '\n\n'.join(translated_parts)
