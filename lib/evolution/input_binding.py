"""Read-only plans binding selected files to the owned frozen run protocol.

prepare_input_binding accepts scope-field paths, never caller-provided digests.
InputBinding.check_current revalidates executor/project/protocol before allocation.
This grants no command approval, execution, scientific pass or effect replay;
command adapters must retain their existing argv/environment admission.
"""
from dataclasses import dataclass
from .contracts import EvolutionStateError


@dataclass(frozen=True)
class InputBinding:
    user_id: int
    run_id: str
    task_id: str
    project_path: str
    protocol_sha256: str
    scope_paths: tuple
    expected_inputs: tuple

    @property
    def expected_input_sha256(self):
        return dict(self.expected_inputs)

    def check_current(self, task):
        if prepare_input_binding(task, dict(self.scope_paths)) != self:
            raise EvolutionStateError('frozen input binding changed; reload owned protocol before execution')


def prepare_input_binding(task, scope_paths):
    from lib.identity import require_user_id
    from .coordination import authorize_task
    from .tool_evidence import normalize_scientific_protocol, scientific_protocol_digest
    from lib.research.observation_audit import normalize_measurement_scope
    run, role, _ = authorize_task(task)
    if role != 'executing' or not run.get('project_path'):
        raise EvolutionStateError('input binding requires the current project executor')
    if not isinstance(scope_paths, dict) or not scope_paths:
        raise EvolutionStateError('input binding requires explicit frozen scope-field paths')
    try:
        protocol = normalize_scientific_protocol(run.get('scientific_protocol'))
        scope = normalize_measurement_scope(protocol.get('measurement_scope'))
    except (ValueError,TypeError) as exc:
        raise EvolutionStateError('input binding requires a frozen measurement scope') from exc
    expected = {}
    for field, path in scope_paths.items():
        if field not in scope or not field.endswith('_digest') or not isinstance(path,str) or not path:
            raise EvolutionStateError('input binding must select frozen digest fields and relative file paths')
        # Use the retained path admission boundary without reading file bytes.
        from .artifact_evidence import _open_project_file
        with _open_project_file(run['project_path'], path):
            pass
        if path in expected and expected[path] != scope[field]:
            raise EvolutionStateError('one input path cannot satisfy conflicting frozen digests')
        expected[path] = scope[field]
    return InputBinding(require_user_id(task['_userId'],context='input binding owner'),
        run['run_id'],task['id'],run['project_path'],scientific_protocol_digest(protocol),
        tuple(sorted(scope_paths.items())),tuple(sorted(expected.items())))
