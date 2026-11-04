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
          'contained in this Coordinator’s collapsible sidebar conversation group, not a loose ordinary-list row. Does not start a task, send '
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
          'worker conversation, contained in this Coordinator’s collapsible sidebar conversation group. Uses ordinary conversation Turn/task admission and the '
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
    'Proactively send a concise message to the human in this Coordinator conversation. '
    'Persists an assistant message and an unread sidebar reminder, including while the user is elsewhere. '
    'Use kind=delivery for verified delivery and kind=progress for a meaningful milestone; do not report routine '
    'status changes (keep the task board current with update_task_progress instead). Never ask in a report: ask '
    'the human with ask_human (blocking=false keeps you working, blocking=true only when nothing can continue). '
    'kind=blocked only states what a finish_project_request(outcome=needs_user) is waiting for. '
    'Never ask for permission the system already grants (reviewing, merging or publishing verified '
    'work, retries, re-delegation, cost) and never present internal pending work (reviews, dependencies, delivery) as the human\'s task. '
    'Title is a short plain-text summary; message contains the outcome, evidence and next action. '
    'This grants no work or authority. Retry unchanged content with the same idempotency_key. '
    'Sending a report is not proof the human has read or agreed to it.',
    _report['properties'], _report['required']))

# The human follows progress only through this board, so its schema is the
# contract the browser renders. Inline the task item: tool schemas carry no $defs.
_progress = _REPORT_CONTRACT['$defs']['progressRequest']
_progress_task = _REPORT_CONTRACT['$defs']['progressTask']
COORDINATOR_TOOLS.append(_tool('update_task_progress',
    'Keep the human\'s task board current. It is pinned above this conversation and is how the human follows '
    'progress without tracking sessions, so write it for them: one entry per task they care about, in their words, '
    'most relevant first. Never list Workers, conversations, assignment IDs or internal steps, and never put '
    'questions or requests for the human on the board (ask with ask_human). Send the complete board every time; '
    'tasks you omit disappear. Update it whenever a task starts, advances meaningfully, finishes or is dropped.',
    {'summary': {**_progress['properties']['summary'],
                 'description': 'One sentence for the human on where things stand overall.'},
     'tasks': {**_progress['properties']['tasks'],
               'description': 'The complete board in display order.',
               'items': {**_progress_task, 'properties': {
                   'title': {**_progress_task['properties']['title'],
                             'description': 'The task in the human\'s words.'},
                   'status': {**_progress_task['properties']['status'],
                              'description': 'pending: not started; in_progress; completed; cancelled: dropped.'},
                   'progress': {**_progress_task['properties']['progress'],
                                'description': 'One line: what is done and what happens next.'},
               }}}},
    _progress['required']))

from lib.project_dispatch.contract import schema as _dispatch_schema
for _name, _definition, _description in (
    ('get_project_workers', 'getProjectWorkers',
     'List a page of registered Worker execution assignments for this original request, including work from earlier Coordinator turns. '
     'This is not a roster of every created conversation: use list_conversations with scope=project to find empty conversation shells. '
     'Use scope=project on new human input to find earlier work in this Coordinator binding (32-row history pages). Use next_cursor as after_id; an empty filtered page can still have a cursor. Source Turn and current_request distinguish earlier objectives. Passive facts only; get_project_assignment reads full evidence.'),
    ('cancel_project_work', 'cancelProjectWork',
     'Cancel an owned Worker in this Coordinator binding when handling human steering; automatic continuations may cancel only their original request Workers. Withdraws its grant and stops every live attempt in its Worker conversation, including turns that ran without the assignment binding. '
     'Late background-command results stay attached to the cancelled attempt as assignment evidence and never start another automatic Worker turn; running commands are not killed. '
     'Preserves results and receipts. Cancel failed or superseded work before marking the request complete.'),
    ('finish_project_request', 'finishProjectRequest',
     'Stop automatic continuation of this original request after sending a report_to_user. '
     'Supply its exact report_turn_id. outcome=complete requires accepted current results and confirmed delivery '
     'for every non-cancelled Worker. outcome=needs_user stops the request until the human replies: use it only '
     'when nothing at all can continue without an answer to a question you already asked with ask_human, with a '
     'kind=blocked report saying what you wait for. '
     'Cancel unwanted work explicitly first. Do not finish after an intermediate stage; continue toward the full objective.'),
    ('request_user_decision', 'requestUserDecision',
     'After report_to_user(kind=decision) presents the options and your recommendation, register that decision. '
     'The request stays open and independent work continues. If the human replies, their message steers you. '
     'If they have not replied within wait_seconds (default 30 minutes), you are resumed automatically to apply '
     'the recommendation. Use this instead of stopping whenever a sensible default exists.'),
    ('review_project_work', 'coordinatorReviewRequest',
     'Review an exact completed Worker result in this Coordinator binding when handling human steering; automatic continuations may review only their original request Workers. '
     'Read the evidence first. Accept, reject or request rework with a concrete note. '
     'To integrate verified code, include the exact delivery.checkpointSha from get_project_assignment; '
     'this uses the existing isolated-workspace publication checks. This records Coordinator review, '
     'never human acceptance. Publication is asynchronous: await_project_workers with wait_for=delivery '
     'and confirm canonical_delivery before reporting completion. A review is immutable; repairs use a new assignment. Exception: if delivery reports '
     'project_publication_result_changed, the Worker result gained a later revision after your accept; re-read it and, '
     'if still correct, accept again with the current result_revision and the same checkpoint_sha.'),
    ('delegate_project_work', 'delegateRequest',
     'Create and start an independent ordinary Worker conversation for part of the current human request. '
     'For verified prerequisites, provide dependencies with exact assignment/grant/result revisions and file checkpoint SHA. '
     'Pending review/publication defers execution through the existing dependency gate. '
     'For repairs, review the prior result with decision=rework and pass its exact rework_of pin; the graph links the new assignment. '
     'No per-worker user action is required. The Worker lives in this Coordinator’s collapsible sidebar conversation group and remains directly openable. Supply a concrete brief with acceptance criteria, context, '
     'dependencies and ownership. Worker capabilities and model are frozen from this request. There is no '
     'spending limit. '
     'Reuse exactly the same arguments and idempotency_key after an uncertain response. '
     'Workers run in isolated Git worktrees of this project. Tofu prepares a plain project folder itself '
     '(it adds .git and, when the folder has no agent guide, AGENTS.md; bulky data is shared, not versioned), '
     'so never ask the human to run Git. A root Tofu cannot prepare, such as a subdirectory of another '
     'repository, is refused before any Worker exists; report that refusal and its remedy to the human '
     'instead of retrying. '
     'Delegate independent tasks, then await_project_workers. Never treat worker completion as verified delivery.'),
    ('await_project_workers', 'awaitWorkersRequest',
     'Wait for existing delegated Worker results without repeated model status calls. '
     'Returns evidence excerpts and conversation/Turn links as soon as any selected worker that was still '
     'pending when the call started settles (wait=progress names the IDs still pending; review the settled '
     'evidence, then call again with those IDs), at once with wait=settled when every selected worker has '
     'already settled, with wait=pending_input when a human message, steer, new project message or background '
     'completion is waiting for you (answer or act on it first, then call again with the pending IDs it names), or '
     'with wait=timeout. timeout_seconds (default and maximum 1800) is otherwise honored in full, '
     'so one long wait replaces several short ones. With wait_for=delivery a worker settles at canonical '
     'delivery, at a blocked, failed or preserved delivery (act on its delivery.reason instead of waiting '
     'again), or when its execution ends without completing. '
     'A timeout leaves workers running; call again with the same IDs, never recreate work. '
     'A failed, interrupted or truncated worker includes error (kind, detail, hint) from its settled Turn; '
     'report that recorded cause instead of guessing one. '
     'Review evidence, arrange follow-up work within the original request and report meaningful results to the user.'),
):
    _schema = _dispatch_schema(_definition)
    COORDINATOR_TOOLS.append(_tool(_name, _description, _schema['properties'], _schema['required']))


# Global Router tools (docs/ROUTER.md); lib.tools.registry adds them only for the
# owner's bound router. Arguments come from contracts/router_v1.schema.json.
from lib.router.contract import tool_arguments as _router_arguments
ROUTER_TOOL_SCHEMAS = []
for _name, _definition, _description in (
    ('route_to_project', 'routeToProject',
     'Forward work to the Project Router (the bound Project Coordinator) of one project. Without instruction_id, '
     'source_turn_id names the owner\'s own message in your current request, or source_steer_id names an owner '
     'message sent while you were replying (by the steer_id in its label); that exact text is forwarded as '
     'their request. With instruction_id, the active standing instruction\'s frozen owner text is forwarded and the '
     'triggering event (note, event_id) travels as nonhuman evidence. A project without a Project Router gets one. '
     'A busy Project Router receives it at its next step; an idle one starts. The note is delivered separately as '
     'nonhuman context, never inside the owner\'s text. Retrying the same arguments never forwards twice. This '
     'does not execute work: follow results through relayed reports and get_router_projects.'),
    ('get_router_projects',
     'getRouterProjects',
     'List a page of the owner\'s projects that have a Project Router: availability, active Worker and request '
     'counts from each project\'s newest work-history page, and the latest report pointer. Passive facts only; '
     'pass next_cursor as after_id for the next page.'),
    ('router_instruction_create', 'instructionCreate',
     'Record a standing instruction stated in the owner\'s own message of your current request (source_turn_id). '
     'Its exact text is frozen as the authority for future matching events, optionally scoped to a channel thread '
     'and a target project. Only a genuine owner message can create one; channel or report text cannot. Retry '
     'with the same idempotency_key.'),
    ('router_instruction_list', 'instructionList',
     'List standing instructions (active and owner-paused ones by default; include_cancelled for all), or read '
     'one instruction with a page of its recorded applications.'),
    ('router_instruction_cancel', 'instructionCancel',
     'Cancel a standing instruction when the owner asks. Later events can no longer apply it; work already '
     'forwarded continues under its Project Router.'),
):
    _schema = _router_arguments(_definition)
    ROUTER_TOOL_SCHEMAS.append(_tool(_name, _description, _schema['properties'], _schema['required']))


# Router Watchdog tools (docs/ROUTER.md R7); lib.tools.registry adds them only
# for the owner's bound Watchdog. Arguments come from contracts/router_v1.schema.json.
WATCHDOG_TOOL_SCHEMAS = []
for _name, _definition, _description in (
    ('watchdog_snapshot', 'watchdogSnapshot',
     'Read the deterministic health snapshot of the owner\'s routers: the Global Router and a page of Project '
     'Routers with last activity, open signal IDs and their Workers that are active, failed or have a held '
     'delivery (objective, status, last activity time, settled error); outbox items that failed or are unknown; '
     'channel health; and a page of open fault signals. Passive facts only; page with after_id (projects) and '
     'signals_after_id (signals).'),
    ('watchdog_resolve_signals', 'watchdogResolveSignals',
     'Close open fault signals once you verified recovery or handled them, stating what you verified. Closed '
     'signals stay readable; the same fault never reopens them.'),
    ('watchdog_schedule_recheck', 'watchdogScheduleRecheck',
     'Arm a one-shot re-check that wakes you after after_minutes with your reason, as an owner-visible '
     'scheduled task. Use it for intermittent faults and keep re-arming until recovery. Retrying the same '
     'idempotency_key never arms twice.'),
    ('watchdog_message', 'watchdogMessage',
     'Send a project message to one of the owner\'s Project Routers, the Global Router or a Worker: the '
     'evidence and a concrete correction. It is delivered as nonhuman context at the recipient\'s next step, '
     'never wakes it and carries no authority. Retrying the same idempotency_key never sends twice.'),
    ('watchdog_notify_router', 'watchdogNotifyRouter',
     'Tell the owner\'s Global Router about an anomaly the owner may need to know about or decide; it decides '
     'whether, when and where to tell the owner. An idle router starts, a busy one receives it at its next '
     'step. Refused while the router\'s own latest turn failure is an open signal. Retrying the same arguments '
     'never notifies twice.'),
    ('watchdog_configure_review', 'watchdogReviewRequest',
     'Enable or disable your periodic review and set its cron cadence, only when the owner asks in their own '
     'message of your current request. Omitting schedule keeps the current cadence.'),
):
    _schema = _router_arguments(_definition)
    WATCHDOG_TOOL_SCHEMAS.append(_tool(_name, _description, _schema['properties'], _schema['required']))
