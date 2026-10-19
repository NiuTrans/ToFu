"""Publish ordinary and atomic native Goal events through one sequence owner.

GoalJournalPublisher is composed by execution_journal. ExecutionJournal holds
the publication lock; the selected native handoff owns its atomic transaction.
Only acknowledged writes advance this cursor. No retry of an ambiguous native
commit, second stream, retained event buffer or execution authority lives here.
"""
from lib.goal_runs.journal_codec import encode_event
from lib.orchestration.execution_journal import RecoveryRefused
from lib.tasks_pkg.persistence_wait import wait_for_persistence


class GoalJournalPublisher:
    def __init__(self, task, runs, metadata, *, cursor):
        self.task, self.runs = task, runs
        self.run_id, self.task_id = metadata['run_id'], metadata['task_id']
        self.owner, self.launch_id = metadata['owner_user_id'], metadata['launch_id']
        self.tenant_id = str(metadata.get('tenant_id') or '')
        self.cursor = cursor
        self.launch_sequence = self.account_sequence = None
        self.account_version = 0

    def __call__(self, event):
        launch_id = (event.get('launch_metadata', {}).get('launch_id')
                     if event['type'] == 'execution_journal_start' else event.get('launch_id'))
        if event['type'] in {'execution_journal_start', 'execution_account_frontier'} and launch_id != self.launch_id:
            raise RecoveryRefused('Goal journal launch binding changed')
        for packet in encode_event(event):
            # Identical ordinary packets reconcile lost ACKs through the same
            # sequence/payload CAS. A conflict never advances this cursor.
            accepted = wait_for_persistence(self.task,
                lambda: self.runs.project_event(self.run_id, self.cursor, packet),
                # Stop fences admission, not evidence already observed or
                # charged. Shutdown returns pending debt to recovery.
                cancellable=event['type'] not in {
                    'execution_effect_completed', 'execution_account_frontier'},
                label=f'Goal execution event {self.run_id}/{self.cursor}')
            if not accepted:
                raise RecoveryRefused('Goal execution event was not durably accepted')
            self.cursor += 1
        # Chunked logical events are addressed by their final reference packet.
        if event['type'] == 'execution_journal_start':
            self.launch_sequence = self.cursor - 1
        elif event['type'] == 'execution_account_frontier':
            self.account_sequence = self.cursor - 1
            self.account_version = event['frontier']['version']

    def commit_account_handoff(self, frontiers, commit, *, tenant_id=''):
        """Keep native account cuts and their receipt in the existing stream.

        The callback returns only after validating the complete native ACK.
        Failed/unknown outcomes leave the cursor untouched and the caller fences
        its journal/account; they cannot become ordinary publication retries.
        """
        if (self.task.get('id') != self.task_id or type(self.task.get('_userId')) is not int
                or self.task.get('_userId') != self.owner
                or self.task.get('_goalRunId') != self.run_id
                or str(self.task.get('_tenant_id') or '') != self.tenant_id
                or str(tenant_id or '') != self.tenant_id
                or self.launch_sequence is None or self.account_sequence is None
                or not isinstance(frontiers, (list, tuple)) or not frontiers):
            raise RecoveryRefused('Goal native handoff has no matching journal binding')
        count = len(frontiers)
        for offset, cut in enumerate(frontiers, 1):
            # Budget owns construction; native admission owns full cut/schema
            # validation. This sequence owner checks only its scope/version,
            # without a second quadratic walk of every actor in every cut.
            if (not isinstance(cut, dict) or type(cut.get('version')) is not int
                    or cut['version'] != self.account_version + offset or cut.get('reason') != 'actor_rebound'
                    or not isinstance(cut.get('snapshot'), dict)
                    or cut['snapshot'].get('root_task_id') != self.task_id
                    or type(cut['snapshot'].get('owner_user_id')) is not int
                    or cut['snapshot']['owner_user_id'] != self.owner):
                raise RecoveryRefused('Goal native handoff account sequence or owner changed')
        commit(dict(target_launch_sequence=self.launch_sequence,
                    target_account_sequence=self.account_sequence, target_cursor=self.cursor))
        self.account_sequence = self.cursor + count - 1
        self.account_version += count
        # Native commits exactly these cuts and one recovery-consumed receipt.
        self.cursor += count + 1
