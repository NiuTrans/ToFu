"""<notranslate>/<nt> block extraction + re-attachment.

Cheap LLMs occasionally drop, mangle, or "localize" the placeholder
brackets — the loose-pattern stripper at the end of
``_reattach_notranslate_blocks`` cleans those leftovers up.
"""

import re

from lib.log import get_logger

logger = get_logger(__name__)


_NOTRANSLATE_RE = re.compile(r'<notranslate>(.*?)</notranslate>', re.DOTALL | re.IGNORECASE)
_NOTRANSLATE_ALIAS_RE = re.compile(r'<nt>(.*?)</nt>', re.DOTALL | re.IGNORECASE)

# In-place placeholder for notranslate blocks. Full-width brackets +
# underscore + digits make this an unusual token that cheap LLMs tend to
# preserve verbatim (unlike `[NT_0]` or `<NT_0>` which can get reformatted /
# escaped). Order-preserving so we can `str.replace(ph, original, 1)` back.
_NT_PLACEHOLDER_FMT = '⟦NT_{}⟧'
_NT_PLACEHOLDER_RE = re.compile(r'⟦NT_(\d+)⟧')
# Tolerant pattern for stripping mangled-but-recognizable placeholder fragments
# that survive into the final output. Cheap LLMs frequently "localize" the
# bracket pair when translating to CJK targets — e.g. swap `⟦⟧` for Chinese
# 【】/〔〕/《》, Japanese 「」/『』, or just `{}` — so the bracket class is
# generous on both sides. Digits accept full-width 0-9 too.
_NT_PLACEHOLDER_LOOSE_RE = re.compile(
    r'[⟦\[\(\{【〔《「『]\s*N\s*T\s*_\s*[\d０-９]+\s*[⟧\]\)\}】〕》」』]',
    re.IGNORECASE,
)


def _extract_notranslate_blocks(text):
    """Replace <notranslate>/<nt> blocks with ⟦NT_N⟧ placeholders.

    Returns (text_with_placeholders, blocks) where ``blocks`` is a list of
    dicts ``{'placeholder': '⟦NT_0⟧', 'content': '...'}`` ordered by
    appearance in the source.  The placeholder is initially emitted at the
    block's source-text position so the LLM has positional context, but
    the prompt explicitly allows the LLM to *reposition* the marker within
    the translated text for target-language fluency (e.g. SVO→SOV word
    order or different adjective placement).  We only require that each
    marker appears exactly once and intact in the output — order is not
    enforced, since the ⟦NT_N⟧ → content mapping is held in Python.
    """
    all_matches = []
    for pattern in [_NOTRANSLATE_RE, _NOTRANSLATE_ALIAS_RE]:
        for m in pattern.finditer(text):
            all_matches.append((m.start(), m.end(), m.group(1)))
    if not all_matches:
        return text, []

    all_matches.sort(key=lambda x: x[0])

    # Walk the original text and emit chunks + placeholders in order so
    # nested / overlapping matches don't double-count. (regex finditer is
    # already non-overlapping, but the two patterns may produce duplicates
    # if someone writes `<notranslate><nt>x</nt></notranslate>`.)
    blocks = []
    out_parts = []
    cursor = 0
    for start, end, content in all_matches:
        if start < cursor:
            # overlapping with a previous match — skip this duplicate
            continue
        out_parts.append(text[cursor:start])
        ph = _NT_PLACEHOLDER_FMT.format(len(blocks))
        blocks.append({'placeholder': ph, 'content': content})
        out_parts.append(ph)
        cursor = end
    out_parts.append(text[cursor:])
    cleaned = ''.join(out_parts).strip()
    return cleaned, blocks


def _reattach_notranslate_blocks(translated, blocks):
    """Substitute ⟦NT_N⟧ placeholders back with their original content.

    If the translation LLM dropped a placeholder (cheap models occasionally
    do this despite the prompt rule), the orphaned content is appended at
    the end with a warning log so it is never silently lost — which is
    strictly no worse than the prior all-suffix behavior.
    """
    if not blocks:
        return translated
    out = translated
    missing = []
    for b in blocks:
        ph = b['placeholder']
        content = b['content']
        if ph in out:
            out = out.replace(ph, content, 1)
        else:
            missing.append(content)
    # Defensive: strip any *partially-mangled* placeholders the LLM may have
    # left behind (e.g. spaces inserted, brackets swapped).
    if _NT_PLACEHOLDER_RE.search(out) or _NT_PLACEHOLDER_LOOSE_RE.search(out):
        leftover = (_NT_PLACEHOLDER_RE.findall(out)
                    + _NT_PLACEHOLDER_LOOSE_RE.findall(out))
        logger.warning('[Translate] notranslate placeholders survived into '
                       'output, stripping: %s', leftover[:5])
        out = _NT_PLACEHOLDER_LOOSE_RE.sub('', out)
        out = _NT_PLACEHOLDER_RE.sub('', out)
    if missing:
        logger.warning('[Translate] %d notranslate block(s) dropped by LLM, '
                       'appending at end as fallback', len(missing))
        out = out.rstrip() + '\n' + '\n'.join(missing)
    return out
