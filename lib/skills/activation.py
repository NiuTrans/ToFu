"""Task-scoped skill selection, bounded disclosure, and content-free receipts.

Entry points: explicit_skill_context for the context composer; record_skill_use
for tool handlers. Reads go through the owner-aware skill registry and loader.
No network, installation, dependency execution, or process-global task state.
"""
from __future__ import annotations

import hashlib
import json
import re

_MENTION = re.compile(r'(?<![\w/$])\$([a-zA-Z][a-zA-Z0-9_-]{0,127})(?![\w-])')
_MAX_SELECTED = 3
_MAX_CONTEXT_TOKENS = 6500
_MAX_RECEIPTS = 32


class SkillSelectionError(ValueError):
    """An explicitly required guide cannot be delivered with its pinned identity."""


def verify_selected_guide(receipt: dict, bindings: list[dict]) -> None:
    for binding in bindings:
        if binding['skill_id'] == receipt['skill_id'] and (
                receipt['status'] != 'loaded' or receipt['content_hash'] != binding['content_hash']):
            raise SkillSelectionError(f"Selected skill {receipt['skill_id']} is unavailable or its guide changed")


def instruction_receipt(skill_id: str, content: str, *, trigger: str) -> dict:
    """Summarize actual disclosure without recording the instruction body."""
    loaded = content.startswith('Skill loaded:')
    digest = re.search(r'^content_hash: ([a-f0-9]+)$', content, re.MULTILINE)
    return {
        'skill_id': skill_id, 'trigger': trigger,
        'status': 'loaded' if loaded else 'unavailable',
        'content_hash': digest[1] if digest else '',
    }


def record_skill_use(task: dict, receipt: dict) -> None:
    receipts = list(task.get('_skillUsage') or [])
    if not receipts or receipts[-1] != receipt:
        receipts.append(dict(receipt))
    task['_skillUsage'] = receipts[-_MAX_RECEIPTS:]


def bind_skill_selection(skill_ids: list[str], *, project_path: str | None = None,
                         owner_user_id: int, expected: list[dict] | None = None) -> list[dict]:
    """Pin explicitly selected installed guides through the ordinary loader.

    Hashes identify SKILL.md, not supporting scripts or scientific correctness.
    This grants no tools, installation, credentials or execution permissions.
    """
    from lib.skills.load import load_skill
    from lib.identity import require_user_id
    require_user_id(owner_user_id, context='skill selection owner')
    if (not isinstance(skill_ids, list) or len(skill_ids) > 16
            or any(not isinstance(sid, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{0,127}', sid)
                   for sid in skill_ids) or len(set(skill_ids)) != len(skill_ids)):
        raise SkillSelectionError('skill selection requires unique installed skill IDs (up to 16)')
    bindings = []
    for sid in skill_ids:
        content = load_skill(sid, project_path=project_path, owner_user_id=owner_user_id)
        receipt = instruction_receipt(sid, content, trigger='bound')
        if receipt['status'] != 'loaded' or not receipt['content_hash']:
            raise SkillSelectionError(f'Selected skill {sid} is missing, disabled or unavailable')
        bindings.append({'skill_id': sid, 'content_hash': receipt['content_hash']})
    if expected is not None and bindings != expected:
        raise SkillSelectionError('Selected skill guide changed; review the selection before creating a new plan')
    return bindings


def explicit_skill_context(query: str, *, task: dict | None,
                           project_path: str | None,
                           extra_paths: list[str], owner_user_id: int | None,
                           model: str = '', required: list[dict] | None = None,
                           review_only: bool = False) -> dict:
    """Resolve exact $ids from the current user request, once per snapshot.

Ordinary mentions disclose at most three guides under one token budget. Required
bindings must all fit in full or refuse; they never silently defer. The loader returns
each guide whole; a guide that cannot fit the combined budget is explicitly
deferred to load_skill, never sliced into a partial instruction block. Model
overflow of a whole guide returned through the tool path is paged by the shared
artifact layer, not by this injector.
"""
    from lib.skills.registry import list_skills
    from lib.skills.load import load_skill
    from lib.skills.injection import _count_tokens

    bounded_query = str(query or '')[:32768]
    fingerprint = hashlib.sha256(json.dumps(
        [bounded_query, owner_user_id, project_path, extra_paths, model, required, review_only],
        ensure_ascii=True).encode()).hexdigest()
    previous = (task or {}).get('_selectedSkillsSnapshot')
    if isinstance(previous, dict) and previous.get('query_hash') == fingerprint:
        return previous
    prose = re.sub(r'```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)', '', bounded_query,
                   flags=re.DOTALL)
    bindings = {row['skill_id']: row['content_hash'] for row in required or []}
    mentions = list(dict.fromkeys([*bindings, *_MENTION.findall(prose)]))[:16]
    result = {'query_hash': fingerprint, 'content': '', 'receipts': []}
    if mentions:
        skills = list_skills(project_path, extra_paths=extra_paths,
                             owner_user_id=owner_user_id)
        by_id = {skill['id']: skill for skill in skills}
        # Shell variables in examples are not requests to activate a skill.
        # Unknown ids get an honest notice only in an explicit skill context.
        if not (bindings or any(name in by_id for name in mentions)
                or re.search(r'\bskills?\b|技能', prose, re.IGNORECASE)
                or prose.strip() in {f'${name}' for name in mentions}):
            mentions = []
        if not mentions:
            if task is not None:
                task['_selectedSkillsSnapshot'] = result
            return result
        parts = ['<selected_skills>',
                 'The user explicitly selected these guides. Their supplied '
                 'instructions are already loaded; follow listed skill:// '
                 'resources only, and treat unnamed paths as workspace files. '
                 'User instructions and tool policy retain precedence.']
        if review_only:
            parts.append('Review-only reference: assess the executor against these guides. '
                         'Do not execute their procedures, repair work or grant additional tools. '
                         'Loading a guide is not evidence that its checks passed.')
        disclosed = max(_MAX_SELECTED, len(bindings))
        for skill_id in mentions[:disclosed]:
            if skill_id not in by_id:
                content = f'Skill not found: {skill_id}. Continue with available tools.'
            else:
                content = load_skill(skill_id, project_path=project_path,
                                     extra_paths=extra_paths,
                                     owner_user_id=owner_user_id)
            receipt = instruction_receipt(skill_id, content,
                                          trigger='bound' if skill_id in bindings else 'explicit')
            verify_selected_guide(receipt, required or [])
            candidate = '\n\n'.join([*parts, content, '</selected_skills>'])
            if _count_tokens(candidate, model) > _MAX_CONTEXT_TOKENS:
                if skill_id in bindings:
                    raise SkillSelectionError('Selected skill guides exceed the context budget; '
                                     'choose a smaller applicable selection. No guide was truncated.')
                content = (f'Selected skill {skill_id} exceeds the remaining '
                           'context budget. Call load_skill with this exact id.')
                receipt['status'] = 'deferred'
            parts.append(content)
            result['receipts'].append(receipt)
        if len(mentions) > disclosed:
            parts.append('Additional selections require load_skill: ' +
                         ', '.join(mentions[disclosed:]))
        parts.append('</selected_skills>')
        result['content'] = '\n\n'.join(parts)
    if task is not None:
        task['_selectedSkillsSnapshot'] = result
    return result
