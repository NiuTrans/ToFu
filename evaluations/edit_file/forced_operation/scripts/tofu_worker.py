"""Persistent hermetic worker: Tofu's production edit_file front door + executor.

Started by run_bench.py with a scratch HOME/TMPDIR/TOFU_DATA_DIR and PYTHONPATH set
to the Tofu worktree. Per request (one JSON line on stdin):
  {"id", "tool_call": {"function": {"name", "arguments"}}, "schema": <offered tool>,
   "path": "<repo-relative file>", "content": "<current text>", "virtual_root": "/workspace/x"}
it writes the file into a fresh project directory, runs
lib.tool_input_repair.ingest_tool_call (decode, curated repair, ToolContractV2
validation against the OFFERED schema, batch-path coverage) and, if admitted, the
unwrapped lib.project_mod.write_tools.tool_edit_file — the same pair the evaluation
harness in evaluations/edit_file/evaluation.py uses, minus the workspace-lease
decorator (its global lock writes fall outside scratch). Replies one JSON line:
  {"id", "output", "content", "admitted", "repair_log", "contract_error"}.
A Python audit hook refuses writes outside the scratch root, sockets and subprocesses.
"""
import inspect
import json
import os
import shutil
import sys
import uuid
from pathlib import Path


def install_guard(root):
    root = Path(root).resolve(strict=True)

    def deny(event):
        raise PermissionError('editbench worker forbids ' + event)

    def check(value, event):
        if isinstance(value, int):
            return
        if not Path(os.fsdecode(value)).resolve().is_relative_to(root):
            deny(event)

    def audit(event, args):
        if event.startswith('socket.') or event in {'subprocess.Popen', 'os.system', 'os.posix_spawn',
                                                    'os.exec', 'os.fork'}:
            deny(event)
        if event == 'open':
            path, mode, flags = args
            if (mode and any(c in mode for c in 'wax+')) or flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                check(path, event)
        elif event in {'os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.truncate',
                       'os.utime'}:
            check(args[0], event)
        elif event in {'os.rename', 'os.link', 'os.symlink'}:
            check(args[0], event)
            check(args[1], event)

    sys.addaudithook(audit)


def to_relative(value, virtual_root):
    if isinstance(value, str) and virtual_root and value.startswith(virtual_root.rstrip('/') + '/'):
        return value[len(virtual_root.rstrip('/')) + 1:]
    return value


def main():
    root = Path(sys.argv[1]).resolve(strict=True)
    install_guard(root)
    from lib.project_mod.tools import _exec_write_file
    from lib.project_mod.write_tools import tool_edit_file
    from lib.tasks_pkg.executor._content_ref import _resolve_content_ref
    from lib.tool_input_repair import ingest_tool_call
    from lib.tools.contracts import compile_execution_contract_documents

    edit_file = inspect.unwrap(tool_edit_file)
    print(json.dumps({'ready': True}), flush=True)
    for line in sys.stdin:
        req = json.loads(line)
        project = root / 'p' / uuid.uuid4().hex
        target = project / req['path']
        target.parent.mkdir(parents=True)
        if req['content'] is not None:
            target.write_text(req['content'], encoding='utf-8')
        reply = {'id': req['id'], 'admitted': False, 'repair_log': [], 'contract_error': None}
        try:
            docs = compile_execution_contract_documents([req['schema']])
            ingested = ingest_tool_call(
                req['tool_call'], known_tools=set(req.get('known_tools') or ['edit_file']),
                model=req.get('model', ''),
                conv_id='editbench-' + req['id'], emit_audit=False,
                contract_documents_by_name=docs)
            reply['repair_log'] = [list(item) for item in (ingested.repair_log or [])]
            reply['contract_error'] = ingested.contract_error
            if ingested.dropped:
                output = f'Tool call dropped ({ingested.drop_reason}).'
            elif ingested.parse_error:
                output = ingested.parse_error
            elif ingested.fn_name == 'write_file':
                args = dict(ingested.fn_args)
                args['path'] = to_relative(args.get('path'), req.get('virtual_root'))
                output = None
                # Mirrors lib/tasks_pkg/handlers/project.py content_ref admission.
                if 'content_ref' in args and 'content' not in args:
                    ref = args.pop('content_ref')
                    resolved = _resolve_content_ref({'toolRounds': req.get('prior_rounds') or []}, ref)
                    if resolved is None:
                        output = (f"Error: content_ref resolution failed — tool_round={ref.get('tool_round', '?')} "
                                  'not found or has no content. Provide explicit content instead.')
                    else:
                        args['content'] = resolved
                if output is None:
                    reply['admitted'] = True
                    output = _exec_write_file(args, str(project), None, None, {})
            elif ingested.fn_name != 'edit_file':
                output = f'Error: unknown tool {ingested.fn_name!r}.'
            else:
                args = dict(ingested.fn_args)
                edits = args.get('edits')
                if isinstance(edits, list):
                    edits = [({**e, 'path': to_relative(e['path'], req.get('virtual_root'))}
                              if isinstance(e, dict) and 'path' in e else e) for e in edits]
                batch = args.get('path') if isinstance(args.get('path'), str) else None
                reply['admitted'] = True
                output = edit_file(str(project), edits,
                                   allow_outside=bool(args.get('allow_outside_workspace')),
                                   atomic=args.get('atomic', True),
                                   batch_path=to_relative(batch, req.get('virtual_root')))
        except Exception as exc:  # noqa: BLE001 - reported to the runner as a harness error
            output = f'HARNESS-ERROR {type(exc).__name__}: {exc}'
            reply['harness_error'] = True
        reply['output'] = output
        reply['content'] = target.read_text(encoding='utf-8') if target.exists() else None
        shutil.rmtree(project, ignore_errors=True)
        print(json.dumps(reply), flush=True)


if __name__ == '__main__':
    main()
