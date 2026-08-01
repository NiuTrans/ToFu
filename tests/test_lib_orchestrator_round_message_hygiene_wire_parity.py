"""Slice 18 wire-parity: _round_message_hygiene.py extraction from _run.py."""

import inspect

from lib.tasks_pkg.orchestrator import _round_message_hygiene


class TestRoundMessageHygieneWireParity:
    """Wire-parity guards for _round_message_hygiene.run_round_message_hygiene."""

    def test_module_exists(self):
        assert _round_message_hygiene is not None

    def test_helper_callable(self):
        assert callable(_round_message_hygiene.run_round_message_hygiene)

    def test_signature_accepts_required_kwargs(self):
        sig = inspect.signature(_round_message_hygiene.run_round_message_hygiene)
        params = set(sig.parameters.keys())
        assert {'task', 'messages', 'round_num', 'tid', 'project_path',
                'project_enabled', 'search_enabled'} <= params

    def test_body_runs_compaction_pipeline(self):
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        assert "run_compaction_pipeline(messages, round_num, task=task)" in src

    def test_body_computes_turn_attachments(self):
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        assert "compute_turn_attachments(" in src
        assert "inject_attachments(" in src

    def test_attachments_skipped_on_round_zero(self):
        """Attachments must be gated on round_num > 0."""
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        assert "if round_num > 0" in src

    def test_attachments_failure_is_non_fatal(self):
        """Attachment bugs must degrade to 'no attachments', never raise."""
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        assert "except Exception" in src
        assert "continuing without attachments" in src

    def test_body_runs_search_addendum_cleanup(self):
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        assert "inject_search_addendum_to_user(" in src
        assert "round_num=round_num" in src

    def test_step_ordering_compaction_before_attachments(self):
        """Compaction must run before attachments (byte-order parity)."""
        src = inspect.getsource(_round_message_hygiene.run_round_message_hygiene)
        i_compact = src.index("run_compaction_pipeline")
        i_attach = src.index("compute_turn_attachments")
        i_addendum = src.index("inject_search_addendum_to_user")
        assert i_compact < i_attach < i_addendum

    def test_docstring_mentions_extraction(self):
        assert "pt_03f4cdf1" in (_round_message_hygiene.__doc__ or "")


class TestRunTaskDelegation:
    def test_run_task_delegates_to_helper(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "run_round_message_hygiene(" in src

    def test_run_task_no_longer_carries_cluster_inline(self):
        from lib.tasks_pkg.orchestrator import _run
        src = inspect.getsource(_run.run_task)
        assert "compute_turn_attachments" not in src
        assert "run_compaction_pipeline(messages" not in src
        assert "inject_search_addendum_to_user(" not in src
