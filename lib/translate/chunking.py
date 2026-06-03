"""Text chunking for parallel translation.

Splits on paragraph boundaries (``\\n\\n``) first; for individual paragraphs
that are still too long, falls back to line-by-line splitting.
"""


def _split_text_for_translation(text, max_chunk=8000):
    """Split text into chunks on paragraph boundaries for chunked translation."""
    if len(text) <= max_chunk:
        return [text]
    chunks = []
    paragraphs = text.split('\n\n')
    current = ''
    for para in paragraphs:
        if current and len(current) + len(para) + 2 > max_chunk:
            chunks.append(current.strip())
            current = para
        else:
            current = current + '\n\n' + para if current else para
    if current.strip():
        chunks.append(current.strip())
    final_chunks = []
    for chunk in chunks:
        if len(chunk) <= max_chunk:
            final_chunks.append(chunk)
        else:
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
    return final_chunks if final_chunks else [text]
