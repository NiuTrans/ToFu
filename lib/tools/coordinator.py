"""Model-visible dispatch-only Coordinator tools; handlers reuse existing owners."""


def _tool(name, description, properties, required):
    return {'type': 'function', 'function': {
        'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties,
                       'required': required, 'additionalProperties': False},
    }}


COORDINATOR_TOOLS = [
    _tool('create_conversation',
          'Create an empty independent conversation in this Coordinator project, '
          'visible in the ordinary conversation list. Does not start a task, send '
          'a user message, or authorize execution. Reuse the same idempotency_key '
          'when retrying. For work within the current authorized request, prefer '
          'delegate_project_work: it creates and starts its own Worker without '
          'a separate per-worker authorization step. Use dispatch_project_assignment '
          'only for an already-authorized assignment.',
          {'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
           'idempotency_key': {'type': 'string', 'minLength': 1, 'maxLength': 200}},
          ['title', 'idempotency_key']),
    _tool('dispatch_project_assignment',
          'Dispatch an existing human-authorized Project assignment into its independent '
          'worker conversation. Uses ordinary conversation Turn/task admission and the '
          'existing exact grant, budget, concurrency and capability limits. Cannot create '
          'or enlarge a grant; reports unauthorized/stale assignments rather than running. '
          'Only the existing grant profile is permitted. For new work within the '
          'current authorized request, use delegate_project_work instead.',
          {'assignment_id': {'type': 'string', 'minLength': 1},
           'expected_revision': {'type': 'integer', 'minimum': 1}},
          ['assignment_id', 'expected_revision']),
    _tool('get_project_assignment',
          'Read one owned same-project worker assignment, execution state and result links. '
          'Passive only: never starts, retries, resumes or wakes a task. Follow its '
          'target_conversation_id with get_conversation for evidence and outcomes.',
          {'assignment_id': {'type': 'string', 'minLength': 1}}, ['assignment_id']),
]


# The same schema governs the tool, publication service and durable retry intent.
from lib.conversations.coordinator_reports import CONTRACT as _REPORT_CONTRACT
_report = _REPORT_CONTRACT['$defs']['reportRequest']
COORDINATOR_TOOLS.append(_tool('report_to_user',
    'Proactively send a concise report to the human in this Coordinator conversation. '
    'Persists an assistant message and an unread sidebar reminder, including while the user is elsewhere. '
    'Use for a decision, blocker, verified delivery or meaningful milestone; do not report routine status changes. '
    'Title is a short plain-text summary; message contains the outcome, evidence and next action. '
    'This grants no work or authority. Retry unchanged content with the same idempotency_key. '
    'Sending a report is not proof the human has read or agreed to it.',
    _report['properties'], _report['required']))

from lib.project_dispatch.contract import schema as _dispatch_schema
for _name, _definition, _description in (
    ('get_project_workers', 'getProjectWorkers',
     'List a page of registered Worker execution assignments for this original request, including work from earlier Coordinator turns. '
     'This is not a roster of every created conversation: use list_conversations with scope=project to find empty conversation shells. '
     'Use scope=project on new human input to find earlier work in this Coordinator binding (32-row history pages). Use next_cursor as after_id; an empty filtered page can still have a cursor. Source Turn and current_request distinguish earlier objectives. Passive facts only; get_project_assignment reads full evidence.'),
    ('cancel_project_work', 'cancelProjectWork',
     'Cancel an owned Worker in this Coordinator binding when handling human steering; automatic continuations may cancel only their original request Workers. Withdraws its grant and stops its exact active attempt. '
     'Preserves results and receipts. Cancel failed or superseded work before marking the request complete.'),
    ('finish_project_request', 'finishProjectRequest',
     'Stop automatic continuation of this original request after sending a report_to_user. '
     'Supply its exact report_turn_id. outcome=complete requires accepted current results and confirmed delivery '
     'for every non-cancelled Worker. Use outcome=needs_user only with a decision or blocked report explaining '
     'the missing human input. Existing Workers keep their limits; cancel unwanted work explicitly first. '
     'Do not finish after an intermediate stage; continue toward the full objective.'),
    ('review_project_work', 'coordinatorReviewRequest',
     'Review an exact completed Worker result in this Coordinator binding when handling human steering; automatic continuations may review only their original request Workers. '
     'Read the evidence first. Accept, reject or request rework with a concrete note. '
     'To integrate verified code, include the exact delivery.checkpointSha from get_project_assignment; '
     'this uses the existing isolated-workspace publication checks. This records Coordinator review, '
     'never human acceptance. Publication is asynchronous: await_project_workers with wait_for=delivery '
     'and confirm canonical_delivery before reporting completion. A review is immutable; repairs use a new assignment.'),
    ('delegate_project_work', 'delegateRequest',
     'Create and start an independent ordinary Worker conversation for part of the current human request. '
     'For verified prerequisites, provide dependencies with exact assignment/grant/result revisions and file checkpoint SHA. '
     'Pending review/publication defers execution through the existing dependency gate. '
     'For repairs, review the prior result with decision=rework and pass its exact rework_of pin; the graph links the new assignment. '
     'No per-worker user action is required. Supply a concrete brief with acceptance criteria, context, '
     'dependencies and ownership. Worker capabilities are frozen from this request; model and aggregate '
     'allowance are server-controlled. Optional limits can narrow the worker allowance. '
     'Reuse exactly the same arguments and idempotency_key after an uncertain response. '
     'Delegate independent tasks, then await_project_workers. Never treat worker completion as verified delivery.'),
    ('await_project_workers', 'awaitWorkersRequest',
     'Wait for existing delegated Worker results without repeated model status calls. '
     'Returns evidence excerpts and conversation/Turn links after all selected workers settle or the wait times out. '
     'A timeout leaves workers running; call again with the same IDs, never recreate work. '
     'Review evidence, arrange follow-up work within the original request and report meaningful results to the user.'),
):
    _schema = _dispatch_schema(_definition)
    COORDINATOR_TOOLS.append(_tool(_name, _description, _schema['properties'], _schema['required']))
