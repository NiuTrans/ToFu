"""Chat controls over the existing durable exploration contract.

Entry: build_exploration_tools. Schemas come from the programme owner; no
catalog, execution policy, lifecycle or storage is defined here.
"""


def available(config, *, conversation_id, owner):
    """Visibility only; execution separately checks the real current human task."""
    return bool(conversation_id and owner and not any(config.get(key) for key in
        ('evolution', '_storageFreeRuntime', '_projectDispatchAdmission')))


def build_exploration_tools():
    from lib.evolution.contracts import inline_program_definition
    return [
        {'type': 'function', 'function': {
            'name': 'get_exploration',
            'description': 'Read an owned long-horizon research/exploration plan, or a page of its experiment receipts. '
                'Use view=catalog without program_id to find existing plans; follow next_cursor with cursor. '
                'Reading never starts or resumes work. Use the same program_id across Chat turns; '
                'detail_path opens the full existing notebook with plans, evidence, independent reviews and costs. '
                'Report confirmed state and unknowns, not invented progress percentages.',
            'parameters': inline_program_definition('chat_read')}},
        {'type': 'function', 'function': {
            'name': 'manage_exploration',
            'description': 'Operate the same durable exploration plan shown in the long-horizon notebook from Chat. '
                'Use when the human requests sustained research, experiments or an independently reviewed deliverable. '
                'For create, supply the agreed objective, acceptance checks and applicable installed skill IDs in request; '
                'use start=true when execution is already requested, otherwise save the plan. Do not send the user to fill another form. '
                'Current Chat project, model and explicitly selected skills are inherited when omitted. '
                'For create/append/guide, reuse idempotency_key and identical arguments after an uncertain response; never create replacement work to retry. '
                'Use command with program_id and expected_version from the latest state_version to start, pause, resume, retry or cancel. '
                'If command acknowledgement is uncertain or the version changed, read current state before deciding again; do not replay an old control. '
                'guide requires expected_run_id from current_experiment and delivers a supplementary instruction to that experiment only; it does not rewrite the frozen original goal. '
                'append adds owner-requested experiments before the original final audit. '
                'Summarize the result in Chat and link detail_path for the full record. '
                'Creation/loading is not acceptance, publication permission or scientific evidence. '
                'The durable plan continues after this Chat turn ends; stopping Chat alone does not cancel it.',
            'parameters': inline_program_definition('chat_write')}},
    ]
