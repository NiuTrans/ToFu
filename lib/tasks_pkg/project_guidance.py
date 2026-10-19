"""Task adapter for complete, path-scoped project instruction delivery.

Tool access acquires applicable ancestor rules without an instruction-delivery
execution gate. Shared round preparation emits one required snapshot through
context_composer; path authorization and command approval retain their owners.
No tool result clipping, separate durable store or recursive scan.
"""
from __future__ import annotations

from pathlib import Path

import os
import threading

from lib.project_mod.instructions import (
    MAX_GUIDANCE_FILES, read_scoped_guidance, render_guidance,
)


def _state(task: dict) -> dict:
    # Tool dispatch can run sibling reads concurrently. setdefault installs one
    # task-owned lock/state; neither survives or cross-contaminates another task.
    return task.setdefault('_projectGuidance', {
        'lock': threading.RLock(), 'rules': {}, 'delivered': {},
    })


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
    from lib.tasks_pkg.workspace_delivery import execution_root_key, ensure_execution_roots
    from lib.project_mod.config import build_root_policy, resolve_namespaced_path
    policy = ensure_execution_roots(task) or build_root_policy(
        config['projectPath'], config.get('projectPaths'), config.get('readOnlyPaths'))
    roots = [Path(row['path']).resolve() for row in policy['roots'].values()]
    paths = []
    for key in ('reads', 'edits'):
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
        # Verified absence removes a previously observed rule on this same
        # target chain, without treating acquisition failures as deletion.
        for path, old in list(candidate.items()):
            if (path not in rows and any(old['root'] == str(root)
                    and selected.is_relative_to(Path(path).parent)
                    for root, selected in targets)):
                del candidate[path]
        if len(candidate) > MAX_GUIDANCE_FILES:
            raise ValueError('Too many applicable rule files; no instructions were omitted')
        render_guidance(list(candidate.values()))  # Validate live rows, not removed versions.
        state['rules'] = candidate
    return ''


def refresh_project_guidance(task: dict, messages: list[dict], *, model: str) -> None:
    config = task.get('config') or {}
    if task.get('aborted') or not config.get('projectPath') or config.get('project_remote'):
        return
    if not task.get('_projectGuidance'):
        from lib.tasks_pkg.workspace_delivery import ensure_execution_roots
        from lib.project_mod.config import build_root_policy
        policy = ensure_execution_roots(task) or build_root_policy(
            config['projectPath'], config.get('projectPaths'), config.get('readOnlyPaths'))
        # Bootstrap at the closed pre-model boundary, not at the first command.
        # The session-level project_rules string carries no raw-file witnesses:
        # deliver a complete scoped snapshot rather than infer ACK from prose.
        check_project_guidance(task, 'read_files', {'reads': [
            {'path': row['path']} for row in policy['roots'].values()]})
    state = _state(task)
    from lib.tasks_pkg.context_composer import (
        ComposeRequest, append_context_blocks, context_block,
    )
    from lib.tasks_pkg.manager import task_user_id
    with state['lock']:
        rows = list(state['rules'].values())

        if not rows and not state['delivered']:
            return
        result = append_context_blocks(messages, [context_block(
            'project_scoped_rules', render_guidance(rows) or 'Previously acquired project guidance was removed; no acquired rules remain.',
            )], ComposeRequest(
                task=task, user_id=task_user_id(task), model=model))
        if any(row.get('injected') and (row.get('appended') or row.get('reused'))
               for row in result.manifest):
            state['delivered'] = {row['path']: row['hash'] for row in rows}
