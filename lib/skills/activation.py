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


def explicit_skill_context(query: str, *, task: dict | None,
                           project_path: str | None,
                           extra_paths: list[str], owner_user_id: int | None,
                           model: str = '') -> dict:
    """Resolve exact $ids from the current user request, once per snapshot.

At most three guides are disclosed, under one token budget. The loader returns
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
        [bounded_query, owner_user_id, project_path, extra_paths, model],
        ensure_ascii=True).encode()).hexdigest()
    previous = (task or {}).get('_selectedSkillsSnapshot')
    if isinstance(previous, dict) and previous.get('query_hash') == fingerprint:
        return previous
    prose = re.sub(r'```.*?(?:```|\Z)|~~~.*?(?:~~~|\Z)', '', bounded_query,
                   flags=re.DOTALL)
    mentions = list(dict.fromkeys(_MENTION.findall(prose)))[:16]
    result = {'query_hash': fingerprint, 'content': '', 'receipts': []}
    if mentions:
        skills = list_skills(project_path, extra_paths=extra_paths,
                             owner_user_id=owner_user_id)
        by_id = {skill['id']: skill for skill in skills}
        # Shell variables in examples are not requests to activate a skill.
        # Unknown ids get an honest notice only in an explicit skill context.
        if not (any(name in by_id for name in mentions)
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
        for skill_id in mentions[:_MAX_SELECTED]:
            if skill_id not in by_id:
                content = f'Skill not found: {skill_id}. Continue with available tools.'
            else:
                content = load_skill(skill_id, project_path=project_path,
                                     extra_paths=extra_paths,
                                     owner_user_id=owner_user_id)
            receipt = instruction_receipt(skill_id, content, trigger='explicit')
            candidate = '\n\n'.join([*parts, content, '</selected_skills>'])
            if _count_tokens(candidate, model) > _MAX_CONTEXT_TOKENS:
                content = (f'Selected skill {skill_id} exceeds the remaining '
                           'context budget. Call load_skill with this exact id.')
                receipt['status'] = 'deferred'
            parts.append(content)
            result['receipts'].append(receipt)
        if len(mentions) > _MAX_SELECTED:
            parts.append('Additional selections require load_skill: ' +
                         ', '.join(mentions[_MAX_SELECTED:]))
        parts.append('</selected_skills>')
        result['content'] = '\n\n'.join(parts)
    if task is not None:
        task['_selectedSkillsSnapshot'] = result
    return result
