"""Task adapter for complete, path-scoped project instruction delivery.

Tool access acquires applicable ancestor rules without an instruction-delivery
execution gate (check_project_guidance). Round preparation
(refresh_project_guidance) delivers each rule file as its own content-addressed
``project_scoped_rules_<instance>`` block through context_composer, so a rule
text enters model-visible history at most once while it stays there:

- unchanged files are never re-sent across rounds or turns; a new task adopts
  and re-validates the rule blocks its inherited history already carries;
- a byte-identical copy at another path (a linked worktree) is a one-line
  scope note naming the delivered copy;
- a changed file appends one superseding version, a deleted one a one-line
  retraction, and history that lost a block gets exactly that block back;
- with nothing changed, a round checks only the identity of the carriers that
  hold the delivered blocks and renders nothing.

Path authorization and command approval retain their owners. No tool result
clipping, separate durable store or recursive scan.
"""
from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import os
import re
import threading

from lib.log import get_logger
from lib.project_mod.instructions import (
    MAX_GUIDANCE_FILES, check_guidance_bounds, read_rule_file, read_scoped_guidance,
    render_rule,
)

logger = get_logger(__name__)

_BLOCK_PREFIX = 'project_scoped_rules'
_CARRIER_MARK = '<!-- tofu-context:' + _BLOCK_PREFIX
_SPAN = re.compile(r'<!-- tofu-context:(project_scoped_rules(?:_[0-9a-f]{16})?):start -->'
                   r'(.*?)<!-- tofu-context:\1:end -->', re.DOTALL)
_SOURCE_LINE = re.compile(r'^Source: (.+)$', re.MULTILINE)
_LEGACY_SOURCES = re.compile(r'^Project Intelligence — [^\n]*\nSource: (.+)$', re.MULTILINE)
# Retires the pre-2026-10-28 combined snapshot (bare block id) once per history.
_LEGACY_NOTE = ('Superseded: project guidance now arrives as one project_scoped_rules_<id> '
                'block per rule file; this combined snapshot is historical.')
_STATUS_NOTES = {
    'deleted': 'deleted; its earlier text no longer applies within {scope}.',
    'outside_roots': 'outside the current workspace roots; its earlier text no longer applies.',
    'unreadable': 'could not be re-read; its earlier text no longer applies within {scope}.',
    'empty': 'now empty; no rules from it apply within {scope}.',
}


def _state(task: dict) -> dict:
    # Tool dispatch can run sibling reads concurrently. setdefault installs one
    # task-owned lock/state; neither survives or cross-contaminates another task.
    return task.setdefault('_projectGuidance', {
        'lock': threading.RLock(), 'rules': {}, 'delivered': {},
        # Paths known only from inherited history carry no new bytes, so they
        # do not count toward this task's acquisition bound.
        'inherited': frozenset(), 'removed': {}, 'version': 0,
        'adopted': False, 'witness': None,
    })


def _root_policy(task: dict) -> dict:
    config = task.get('config') or {}
    from lib.tasks_pkg.workspace_delivery import ensure_execution_roots
    from lib.project_mod.config import build_root_policy
    return ensure_execution_roots(task) or build_root_policy(
        config['projectPath'], config.get('projectPaths'), config.get('readOnlyPaths'))


def _rule_block_id(path: str) -> str:
    # The composer orders one append by block id: the depth prefix puts root
    # rules before the nested rules that specialize them.
    depth = min(len(Path(path).parts), 99)
    return f'{_BLOCK_PREFIX}_{depth:02d}{sha256(os.fsencode(path)).hexdigest()[:14]}'


def _status_note(path: str, status: str) -> str:
    return f'Source: {path} — ' + _STATUS_NOTES[status].format(scope=Path(path).parent)


def freeze_project_command_cwd(task: dict, arguments: dict) -> dict:
    """Select sticky/explicit cwd once before rules, callbacks or background handoff."""
    config = task.get('config') or {}
    if not config.get('projectPath') or config.get('project_remote'):
        return arguments
    from lib.tasks_pkg.workspace_delivery import execution_root_key, ensure_execution_roots
    from lib.project_mod.config import get_conv_cwd, execution_root_scope
    from lib.project_mod.path_resolution import _resolve_base
    key = execution_root_key(task)
    with execution_root_scope(key, ensure_execution_roots(task)):
        selected = arguments.get('working_dir') or get_conv_cwd(key) or config['projectPath']
        base, relative = _resolve_base(config['projectPath'], selected, conv_id=key)
    return {**arguments, 'working_dir': str((Path(base) / relative).expanduser().resolve())}


def check_project_guidance(task: dict, tool_name: str, arguments: dict) -> str:
    """Acquire rules for the next model request without refusing tool execution."""
    config = task.get('config') or {}
    if not config.get('projectPath') or config.get('project_remote'):
        return ''
    from lib.tasks_pkg.workspace_delivery import execution_root_key
    from lib.project_mod.config import resolve_namespaced_path
    roots = [Path(row['path']).resolve() for row in _root_policy(task)['roots'].values()]
    paths = []
    for key in ('reads', 'edits', 'searches'):
        for entry in arguments.get(key) or []:
            if isinstance(entry, dict) and isinstance(entry.get('path'), str):
                paths.append(entry['path'])
    if isinstance(arguments.get('path'), str):
        paths.append(arguments['path'])
    if tool_name == 'run_command':
        from lib.project_mod.config import get_conv_cwd
        cwd = arguments.get('working_dir') or get_conv_cwd(execution_root_key(task)) or config['projectPath']
        paths.append(cwd)
        from lib.project_mod.path_resolution import _resolve_base
        from lib.project_mod.command_analysis import (
            command_segment_targets, _split_pipeline_spans, _mask_quoted_literals,
            shell_source_has_expansion,
        )
        base, relative = _resolve_base(config['projectPath'], cwd, conv_id=execution_root_key(task))
        active_cwd = (Path(base) / relative).expanduser().resolve()
        # Reuse the parser's retained literal candidates, not its all-or-none
        # snapshot precision. Expansion in printf's data cannot hide its redirect.
        # Opaque interpreter effects remain cooperative, not a sandbox proof.
        # Unknown shell targets are not instruction-delivery refusals; execution
        # authorization remains in the command and path policy owners.
        command = arguments.get('command') or ''
        spans = _split_pipeline_spans(command)
        cwds = {active_cwd}
        for index, (start, end) in enumerate(spans):
            segment = command[start:end].strip()
            parsed = command_segment_targets(segment)
            words = list(parsed.words)
            next_separator = command[end:spans[index + 1][0]].strip() if index + 1 < len(spans) else ''
            previous_separator = command[spans[index - 1][1]:start].strip() if index else ''
            target_cwds = set(cwds)
            if words[:1] == ['cd']:
                args = words[1:]
                physical = False
                while args and args[0] in {'-P', '-L', '--'}:
                    option, args = args[0], args[1:]
                    if option == '--':
                        break
                    physical = option == '-P'
                if (len(args) != 1 or shell_source_has_expansion(segment)
                        or any(mark in _mask_quoted_literals(command) for mark in '(){}')):
                    continue
                destinations = {(cwd / Path(args[0]).expanduser()).resolve() if physical
                    else Path(os.path.abspath(cwd / Path(args[0]).expanduser())) for cwd in cwds}
                paths.extend(str(path) for path in destinations)
                if previous_separator != '|' and next_separator != '|':
                    cwds = destinations if next_separator == '&&' else cwds | destinations
            for target in parsed.targets:
                if shell_source_has_expansion(target):
                    continue
                paths.extend(str((cwd / Path(target).expanduser()).resolve()) for cwd in target_cwds | cwds)
    if not paths:
        return ''
    rows = {}
    targets = []
    for raw in dict.fromkeys(paths):
        if raw.startswith('file://'):
            raw = raw[7:]
        if Path(raw).is_absolute() or raw.startswith('~'):
            selected = Path(raw).expanduser().resolve()
        else:
            base, relative = resolve_namespaced_path(raw, execution_root_key(task))
            selected = (Path(base) / relative).resolve()
        # Unknown outside targets still go through the existing authorization
        # gate. Discovery here never adopts a tree or reads its instructions.
        root = max((root for root in roots if selected.is_relative_to(root)),
                   key=lambda root: len(str(root)), default=None)
        if root is None:
            continue
        targets.append((root, selected))
        for row in read_scoped_guidance(str(root), str(selected)):
            rows[row['path']] = row
    state = _state(task)
    with state['lock']:
        candidate = {**state['rules'], **rows}
        removed = {path: reason for path, reason in state['removed'].items() if path not in rows}
        # Verified absence removes a previously observed rule on this same
        # target chain, without treating acquisition failures as deletion.
        for path, old in list(candidate.items()):
            if (path not in rows and any(old['root'] == str(root)
                    and selected.is_relative_to(Path(path).parent)
                    for root, selected in targets)):
                del candidate[path]
                removed[path] = 'deleted'
        inherited = frozenset(path for path in state['inherited']
                              if path in candidate and path not in rows)
        if inherited != state['inherited'] or _rules_differ(state['rules'], candidate):
            acquired = [row for path, row in candidate.items() if path not in inherited]
            if len(acquired) > MAX_GUIDANCE_FILES:
                raise ValueError('Too many applicable rule files; no instructions were omitted')
            check_guidance_bounds(acquired)  # Validate live rows, not removed versions.
        _commit_rules(state, candidate, removed, inherited)
    return ''


def _rules_differ(old: dict, new: dict) -> bool:
    return old.keys() != new.keys() or any(
        row['hash'] != old[path]['hash'] for path, row in new.items())


def _commit_rules(state: dict, rules: dict, removed: dict, inherited: frozenset) -> None:
    """Install acquired state; any model-visible difference invalidates the witness."""
    if removed != state['removed'] or _rules_differ(state['rules'], rules):
        state['version'] += 1
    state['rules'], state['removed'], state['inherited'] = rules, removed, inherited


def refresh_project_guidance(task: dict, messages: list[dict], *, model: str) -> None:
    """Deliver exactly the rule blocks the model-visible history is missing."""
    config = task.get('config') or {}
    if task.get('aborted') or not config.get('projectPath') or config.get('project_remote'):
        return
    if not task.get('_projectGuidance'):
        # Bootstrap at the closed pre-model boundary, not at the first command.
        # The session-level project_rules string carries no raw-file witnesses:
        # deliver complete scoped rules rather than infer ACK from prose.
        check_project_guidance(task, 'read_files', {'reads': [
            {'path': row['path']} for row in _root_policy(task)['roots'].values()]})
    state = _state(task)
    if not state['adopted']:
        _adopt_history(task, state, messages)
    with state['lock']:
        if _witness_holds(state, messages):
            return
        _deliver(task, state, messages, model)


def _adopt_history(task: dict, state: dict, messages: list[dict]) -> None:
    """Re-validate, once per task, the rule blocks inherited from earlier turns.

    Their text is already visible, so unchanged files cost nothing; a file that
    changed, vanished or left the declared roots since then gets a superseding
    version or a retraction instead of silently staying current.
    """
    found, gone = {}, {}
    sources = _history_sources(messages)
    roots = ([Path(row['path']).resolve() for row in _root_policy(task)['roots'].values()]
             if sources else [])
    for path in sources:
        root = max((root for root in roots if Path(path).is_relative_to(root)),
                   key=lambda root: len(str(root)), default=None)
        if root is None:
            gone[path] = 'outside_roots'
            continue
        try:
            row = read_rule_file(path, str(root))
        except (OSError, ValueError) as error:
            logger.warning('[ProjectGuidance] inherited rule %s cannot be re-read: %s', path, error)
            gone[path] = 'unreadable'
            continue
        if row is None:
            gone[path] = 'deleted'
        else:
            found[path] = row
    with state['lock']:
        fresh = {path: row for path, row in found.items() if path not in state['rules']}
        removed = {**{path: reason for path, reason in gone.items()
                      if path not in state['rules']}, **state['removed']}
        _commit_rules(state, {**state['rules'], **fresh}, removed,
                      state['inherited'] | frozenset(fresh))
        state['adopted'] = True


def _carrier_texts(message) -> list[str]:
    if not (isinstance(message, dict) and message.get('_contextComposer')):
        return []
    content = message.get('content')
    parts = content if isinstance(content, list) else [content]
    texts = [part.get('text') if isinstance(part, dict) else part for part in parts]
    return [text for text in texts if isinstance(text, str) and _CARRIER_MARK in text]


def _history_sources(messages: list[dict]) -> list[str]:
    """Rule paths whose latest delivered block version is in model-visible history."""
    latest = {}
    for message in messages:
        for text in _carrier_texts(message):
            for match in _SPAN.finditer(text):
                latest[match.group(1)] = match.group(2)
    sources = []
    for block_id, body in latest.items():
        if block_id == _BLOCK_PREFIX:
            sources.extend(_LEGACY_SOURCES.findall(body))
            continue
        line = _SOURCE_LINE.search(body)
        path = line.group(1).split(' — ', 1)[0] if line else ''
        if path and _rule_block_id(path) == block_id:
            sources.append(path)
    return list(dict.fromkeys(sources))


def _planned_texts(state: dict, present: dict, digest) -> dict[str, str]:
    """The text each guidance block must currently carry, keyed by block id.

    Identical bytes keep one full copy: preferably one history already holds,
    otherwise the first acquired path. Every other path is a scope note.
    """
    rules = state['rules']
    full = {path: render_rule(row) for path, row in rules.items()}
    ids = {path: _rule_block_id(path) for path in rules}
    in_history = {path for path, text in full.items()
                  if text and ids[path] in present and present[ids[path]] == digest(ids[path], text)}
    holders = {}
    for path, row in rules.items():
        if full[path] and (row['hash'] not in holders or (
                path in in_history and holders[row['hash']] not in in_history)):
            holders[row['hash']] = path
    texts = {}
    for path, row in rules.items():
        holder = holders.get(row['hash'])
        if not full[path]:
            if ids[path] in present:
                texts[ids[path]] = _status_note(path, 'empty')
        elif holder == path:
            texts[ids[path]] = full[path]
        else:
            texts[ids[path]] = (f'Source: {path} — same bytes as {holder}; that text '
                                f'applies within {row["scope"]} too and is not repeated here.')
    for path, reason in state['removed'].items():
        if _rule_block_id(path) in present:
            texts[_rule_block_id(path)] = _status_note(path, reason)
    if _BLOCK_PREFIX in present:
        texts[_BLOCK_PREFIX] = _LEGACY_NOTE
    return texts


def _deliver(task: dict, state: dict, messages: list[dict], model: str) -> None:
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, append_context_blocks, block_content_hash, context_block,
        verified_block_hashes,
    )
    from lib.tasks_pkg.manager import task_user_id
    version = state['version']
    present = {block_id: digest for block_id, digest in verified_block_hashes(messages).items()
               if block_id.startswith(_BLOCK_PREFIX)}
    # A retraction is owed only while an earlier version is still visible.
    state['removed'] = {path: reason for path, reason in state['removed'].items()
                        if _rule_block_id(path) in present}
    blocks = [context_block(_BLOCK_PREFIX, text, instance=block_id[len(_BLOCK_PREFIX) + 1:])
              for block_id, text in _planned_texts(state, present, block_content_hash).items()
              if present.get(block_id) != block_content_hash(block_id, text)]
    if blocks:
        result = append_context_blocks(messages, blocks, ComposeRequest(
            task=task, user_id=task_user_id(task), model=model))
        landed = {row['id'] for row in result.manifest
                  if row.get('injected') and (row.get('appended') or row.get('reused'))}
        if not {block.id for block in blocks} <= landed:
            # Deferred at an open tool batch or aborted: retry at the next boundary.
            state['witness'] = None
            return
    state['delivered'] = {path: row['hash'] for path, row in state['rules'].items()}
    state['witness'] = {'version': version, 'carriers': _rule_carriers(messages)}


def _rule_carriers(messages: list[dict]) -> list[tuple]:
    carriers = []
    for message in messages:
        if _carrier_texts(message):
            content = message.get('content')
            parts = content if isinstance(content, list) else [content]
            carriers.append((message, content, tuple(
                part.get('text') if isinstance(part, dict) else part for part in parts)))
    return carriers


def _witness_holds(state: dict, messages: list[dict]) -> bool:
    """Unchanged rules plus untouched carrier objects prove delivery is current.

    The witness holds strong references, so no other live object can reuse a
    carrier's identity. A compactor that drops, copies or edits any carrier
    forces the content-addressed verification in _deliver.
    """
    witness = state['witness']
    if witness is None or witness['version'] != state['version']:
        return False
    live = {id(message) for message in messages}
    for message, content, texts in witness['carriers']:
        if (id(message) not in live or not message.get('_contextComposer')
                or message.get('content') is not content):
            return False
        parts = content if isinstance(content, list) else [content]
        if len(parts) != len(texts) or any(
                (part.get('text') if isinstance(part, dict) else part) is not text
                for part, text in zip(parts, texts)):
            return False
    return True
