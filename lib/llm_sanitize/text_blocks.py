"""Pure provider-neutral text-block normalization (no transport dependencies).

``normalize_text_blocks`` folds blank separators only within runs of plain
``{type, text}`` blocks. Metadata-bearing and non-text blocks are hard
boundaries: cache prefixes, signatures and opaque state must never be merged.
The input list/dicts are not mutated; only changed text blocks are copied.
"""


def is_blank_text_block(block) -> bool:
    """Identify provider-invalid blank text without accepting malformed text."""
    if not isinstance(block, dict) or block.get('type') != 'text':
        return False
    text = block.get('text') or ''
    return isinstance(text, str) and not text.strip()


def normalize_text_blocks(blocks: list) -> list:
    """Return valid blocks, preserving exact whitespace beside compatible text.

    Compatibility requires exactly ``type`` and ``text`` keys with string
    text. Nonblank blocks remain separate. Whitespace folds left when possible,
    otherwise right at the start of a plain run; a wholly blank run is dropped.
    Empty strings contribute no bytes. Blank metadata-bearing blocks are
    dropped, never transferred, and still interrupt runs. No separators are
    injected, and no bytes move across multimodal/metadata boundaries.
    """
    out = []
    pending = []
    previous_plain_text = False

    def fold_left():
        if pending and previous_plain_text:
            block = out[-1]
            out[-1] = {**block, 'text': block['text'] + ''.join(pending)}
        pending.clear()

    for block in blocks:
        plain = (isinstance(block, dict) and block.get('type') == 'text'
                 and block.keys() == {'type', 'text'}
                 and isinstance(block['text'], str))
        if not plain:
            fold_left()
            previous_plain_text = False
            if not is_blank_text_block(block):
                out.append(block)
            continue

        text = block['text']
        if not text.strip():
            if text:
                pending.append(text)
            continue

        if previous_plain_text:
            fold_left()
        elif pending:
            block = {**block, 'text': ''.join(pending) + text}
            pending.clear()
        out.append(block)
        previous_plain_text = True

    fold_left()
    return out
