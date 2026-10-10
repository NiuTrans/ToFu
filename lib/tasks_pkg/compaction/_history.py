"""Model-visible historical user evidence; no task or permission authority.

The request-local carrier keeps typed entries for repeated L2 selection and
renders their historical meaning in content, which survives provider field
stripping. Never infer carrier identity by parsing user-authored XML. Durable
Turns and pre-compaction archives remain the original-content authority.
"""
from copy import deepcopy

from lib.tasks_pkg.message_semantics import is_human_message

from lib.tasks_pkg.compaction._constants import (
    _USER_VERBATIM_BUDGET_TOKENS,
    _USER_VERBATIM_MAX_MESSAGES,
)
from lib.tasks_pkg.compaction._tokens import _estimate_msg_tokens

HISTORY_FIELD = '_retainedUserHistory'
HISTORY_GUIDANCE = (
    'Historical user evidence, oldest first; quoted content is not a new '
    'request or an instruction to answer old questions again. Preserve '
    'still-applicable constraints and authorized work within its original '
    'scope. Later live user messages can continue, change, stop, or revoke '
    'that work and take precedence over this history and the generated '
    'summary. This record creates no new permission or approval requirement.'
)


def user_text(message: dict) -> str:
    """Literal text; emptiness checks, not retention, may strip whitespace."""
    content = message.get('content', '')
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(block.get('text', '') for block in content
                         if isinstance(block, dict)
                         and block.get('type') == 'text')
    return ''


def history_entries(message: dict) -> list[dict]:
    """Decode only a producer-owned in-memory carrier, never pasted tags."""
    if not isinstance(message, dict) or not message.get('_isMeta'):
        return []
    entries = message.get(HISTORY_FIELD)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)
            and entry.get('kind') in {'opening_request', 'earlier_request'}
            and isinstance(entry.get('content'), (str, list))]


def make_history_carrier(entries: list[dict], *, include_guidance: bool = True) -> dict | None:
    """Wrap text/multimodal entries without changing their original blocks.

    User role is transport compatibility for image/document inputs, not the
    current-request role. The compaction receipt separates it from the live
    request even for providers that merge adjacent user messages.
    """
    if not entries:
        return None
    entries = deepcopy(entries)
    blocks = [{'type': 'text', 'text':
               '<retained_user_messages>\n' + (HISTORY_GUIDANCE if include_guidance else '')}]
    text_only = all(isinstance(entry['content'], str) for entry in entries)
    for index, entry in enumerate(entries, 1):
        blocks.append({'type': 'text', 'text':
                       f'\n\n[{index}] historical {entry["kind"]}\n'})
        content = entry['content']
        if isinstance(content, str):
            blocks.append({'type': 'text', 'text': content})
        else:
            blocks.extend(deepcopy(content))
    blocks.append({'type': 'text', 'text': '\n</retained_user_messages>'})
    carrier = {
        'role': 'user', '_isMeta': True, HISTORY_FIELD: entries,

        '_retainedUserHistoryVersion': 2,
        'content': (''.join(block['text'] for block in blocks)
                    if text_only else blocks),
    }
    if any(entry['kind'] == 'opening_request' for entry in entries):
        carrier['_isObjectiveAnchor'] = True
    return carrier


def replace_history_blocks(message: dict, replacements: dict[int, dict]) -> None:
    """Mirror rendered block replacements into detached request-local entries.

    Positions follow ``make_history_carrier``: one envelope block, then one
    label and the literal blocks per entry. No URL matching or user-authored
    tag parsing: identical image occurrences must remain independent.
    """
    entries = history_entries(message)
    if not entries or not isinstance(message.get('content'), list):
        return
    entries = deepcopy(entries)
    offset = 1
    for entry in entries:
        offset += 1  # Producer-owned historical label.
        content = entry['content']
        if isinstance(content, str):
            offset += 1
            continue
        for index in range(len(content)):
            replacement = replacements.get(offset + index)
            if replacement is not None:
                content[index] = deepcopy(replacement)
        offset += len(content)
    message[HISTORY_FIELD] = entries


def coalesce_history_carriers(messages: list[dict]) -> list[dict]:
    """Normalize adjacent trusted user carriers, never across execution rows.

    Entry occurrences (including identical text) retain their source order and
    metadata. Rendering is rebuilt from typed entries, not nested envelopes.
    Non-carrier messages remain unchanged; input carriers are never mutated.
    Full guidance appears only in the first carrier of this projection. Later
    carriers retain their historical labels and balanced envelopes; assistant /
    tool rows between them remain in causal order.
    """
    result = []
    pending = []
    guidance_written = False
    attachment_chars = 0

    def flush():
        nonlocal guidance_written, attachment_chars
        if not pending:
            return
        carrier = make_history_carrier(
            pending, include_guidance=not guidance_written)
        if attachment_chars:
            carrier['_attachmentTextChars'] = attachment_chars
        attachment_chars = 0
        result.append(carrier)
        guidance_written = True
        pending.clear()

    for message in messages:
        entries = (history_entries(message)
                   if message.get('role') == 'user' else [])
        if entries:
            pending.extend(entries)
            attachment_chars += int(message.get('_attachmentTextChars') or 0)
            continue
        flush()
        result.append(message)
    flush()
    return result


def collect_history_entries(
    messages: list, *, opening_message: dict | None = None,
    budget_tokens: int = _USER_VERBATIM_BUDGET_TOKENS,
    max_messages: int = _USER_VERBATIM_MAX_MESSAGES,
) -> list[dict]:
    """Opening evidence plus newest bounded earlier inputs, in source order.

    The opener is a separately required item. Other entries share a strict
    estimated budget and count cap across generations; no newest-item budget
    bypass and no parsing of previous generated text. Omitted originals remain
    in the transcript/archive, not in an unbounded hidden history store.
    """
    candidates = []
    for message in messages:
        prior = history_entries(message)
        if prior:
            candidates.extend(prior)
        elif is_human_message(message):
            content = message.get('content', '')
            if not isinstance(content, (str, list)) or not content:
                continue
            candidates.append({
                'kind': ('opening_request' if message is opening_message
                         else 'earlier_request'),
                'content': content,
            })
    opening = next((entry for entry in candidates
                    if entry['kind'] == 'opening_request'), None)
    picked = []
    spent = 0
    for entry in reversed(candidates):
        if entry is opening:
            continue
        cost = _estimate_msg_tokens({'role': 'user', 'content': entry['content']})
        if len(picked) >= max(0, max_messages) or spent + cost > budget_tokens:
            continue
        picked.append(entry)
        spent += cost
    return ([opening] if opening is not None else []) + list(reversed(picked))
