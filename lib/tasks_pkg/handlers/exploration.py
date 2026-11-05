"""Canonical tool adapter for human Chat exploration controls."""
import json

from lib.tasks_pkg.handlers._adapter import simple_call


def handle_exploration(task, tc, fn_name, tc_id, fn_args, rn, round_entry,
                       cfg, project_path, project_enabled, all_tools=None):
    from lib.evolution.chat_programs import execute
    from lib.evolution.contracts import EvolutionStateError
    from lib.storage.errors import StorageError
    def call(name, args):
        try:
            return json.dumps(execute(task, name, args), ensure_ascii=False)
        except (PermissionError, ValueError, EvolutionStateError, StorageError) as error:
            from lib.tools.result_envelope import typed_tool_error
            from lib.tools.result_value import TrustedResultText, capture_result
            content = typed_tool_error('exploration_control_error', retryable=False, message=str(error),
                next_action='Read the current exploration state before deciding again. After an uncertain create/append/guide, reuse the original idempotency_key and arguments; do not create replacement work.').to_envelope_text()
            return TrustedResultText(content, canonical=capture_result(content, execution_status='error'), envelope=True)
    return simple_call(task, fn_name, fn_args, rn, round_entry, tc_id,
                       executor=call, source='Exploration', module_tag='Exploration')
