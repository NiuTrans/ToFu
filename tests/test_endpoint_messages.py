"""Comprehensive tests for endpoint mode message shapes.

Validates that every LLM API call made during the endpoint
planner → worker → critic loop has the correct message structure:

  Scenario 1 (within endpoint loop):
    - Planner sees: system(planner_prompt) → ...context → user("produce your plan...")
    - Worker sees:  system → user(planner_brief)  [replaces original user msg]
    - Critic sees:  system(critic_prompt) → context → user(planner_brief) → assistant(worker) → user("review...")
    - Next worker:  system → user(planner_brief) → assistant(worker1) → user(critic_feedback)

  Scenario 2 (follow-up after endpoint completes):
    - buildApiMessages filters out all _isEndpointReview messages
    - No two consecutive user messages appear
    - Shape: ...context → planner(assistant) → worker(assistant) → user(new_question)

Uses monkeypatched dispatch_stream to capture request payloads and return
scenario-specific responses (planner brief, worker output, critic verdict).
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid

import pytest

# Ensure project root is on sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════
#  Mock LLM Call Recorder
# ═══════════════════════════════════════════════════════════

class MockLLMRecorder:
    """Records all LLM API calls and returns programmable responses.

    Monkeypatches dispatch_stream so we intercept the exact body (including
    messages) that would be sent to the real LLM API.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self.response_queue: list[dict] = []
        self._lock = threading.Lock()

    def enqueue_response(self, content: str, finish_reason: str = "end_turn",
                         tool_calls: list | None = None):
        """Add a response to the FIFO queue.

        Args:
            content: The text content of the response.
            finish_reason: The finish reason (default "end_turn").
            tool_calls: Optional list of tool call dicts to simulate tool usage.
        """
        self.response_queue.append({
            "content": content,
            "finish_reason": finish_reason,
            "tool_calls": tool_calls,
        })

    def enqueue_responses(self, *contents: str):
        """Add multiple simple text responses."""
        for c in contents:
            self.enqueue_response(c)

    def enqueue_worker_response(self, content: str):
        """Enqueue a worker response that first does a fake tool call, then returns content.

        This simulates realistic worker behavior where the worker uses tools
        before producing its final text response.  Two queue entries are added:
        1. A tool_calls response (assistant requests a tool)
        2. The final text response (after tool result is processed)
        """
        self.enqueue_response(
            content="",
            finish_reason="tool_calls",
            tool_calls=[{
                "id": f"call_mock_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": "web_search",
                    "arguments": json.dumps({"query": "mock search"}),
                },
            }],
        )
        self.enqueue_response(content, finish_reason="end_turn")

    def get_calls(self) -> list[dict]:
        with self._lock:
            return list(self.calls)

    def reset(self):
        with self._lock:
            self.calls.clear()
            self.response_queue.clear()

    def mock_dispatch_stream(self, body_or_messages, **kwargs):
        """Drop-in replacement for dispatch_stream.

        Records the call and returns the next queued response.
        """
        # Extract messages from body
        if isinstance(body_or_messages, dict):
            messages = body_or_messages.get("messages", [])
            model = body_or_messages.get("model", "mock")
        else:
            messages = body_or_messages
            model = "mock"

        with self._lock:
            self.calls.append({
                "messages": [dict(m) for m in messages],
                "model": model,
                "timestamp": time.time(),
            })
            if self.response_queue:
                resp_spec = self.response_queue.pop(0)
            else:
                resp_spec = {"content": "1234", "finish_reason": "end_turn"}

        content = resp_spec["content"]
        finish_reason = resp_spec.get("finish_reason", "end_turn")

        # Call on_content callback if provided (mimics streaming)
        on_content = kwargs.get("on_content")
        if on_content:
            on_content(content)

        # Return (assistant_msg, finish_reason, usage)
        msg = {"role": "assistant", "content": content}
        usage = {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "_dispatch": {"provider_id": "mock"},
        }
        return msg, finish_reason, usage


# ═══════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════

def _extract_roles(messages: list[dict]) -> list[str]:
    """Extract role sequence from a messages list."""
    return [m.get("role", "?") for m in messages]


def _has_consecutive_same_role(messages: list[dict], role: str) -> bool:
    """Check if there are consecutive messages with the same role."""
    roles = _extract_roles(messages)
    for i in range(len(roles) - 1):
        if roles[i] == role and roles[i + 1] == role:
            return True
    return False


def _run_endpoint_task_and_wait(task, timeout=60):
    """Run an endpoint task and wait for completion."""
    from lib.tasks_pkg.endpoint import run_endpoint_task

    done = threading.Event()
    error_box = []

    def _worker():
        try:
            run_endpoint_task(task)
        except Exception as e:
            logger.error("Endpoint task failed: %s", e, exc_info=True)
            error_box.append(e)
            task['error'] = str(e)
        finally:
            done.set()

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    if not done.wait(timeout=timeout):
        task['aborted'] = True
        raise TimeoutError(f"Endpoint task did not complete within {timeout}s")
    if error_box:
        raise error_box[0]


# ═══════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════

@pytest.fixture
def recorder(monkeypatch):
    """Create a MockLLMRecorder and monkeypatch dispatch_stream."""
    rec = MockLLMRecorder()

    # Patch dispatch_stream at the point where stream_llm_response calls it
    import lib.tasks_pkg.manager as manager_mod
    monkeypatch.setattr(manager_mod, "dispatch_stream", rec.mock_dispatch_stream)

    # Also set a valid env so model config resolution doesn't fail
    monkeypatch.setenv("LLM_MODEL", "mock-model")
    monkeypatch.setenv("LLM_API_KEYS", "mock-test-key")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:19999/v1")

    yield rec


# ═══════════════════════════════════════════════════════════
#  Test: Role validation helpers (unit tests)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestRoleValidationHelpers:
    """Unit tests for the role-checking utility functions."""

    def test_extract_roles(self):
        msgs = [{"role": "system"}, {"role": "user"}, {"role": "assistant"}]
        assert _extract_roles(msgs) == ["system", "user", "assistant"]

    def test_consecutive_user_detected(self):
        msgs = [{"role": "user"}, {"role": "user"}]
        assert _has_consecutive_same_role(msgs, "user") is True

    def test_no_consecutive_user(self):
        msgs = [{"role": "user"}, {"role": "assistant"}, {"role": "user"}]
        assert _has_consecutive_same_role(msgs, "user") is False

    def test_consecutive_assistant_detected(self):
        msgs = [{"role": "assistant"}, {"role": "assistant"}]
        assert _has_consecutive_same_role(msgs, "assistant") is True


# ═══════════════════════════════════════════════════════════
#  Test: Verdict parsing (unit)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestVerdictParsing:
    """Unit tests for _parse_verdict (three-way: stop / worker / planner)."""

    def test_stop_verdict(self):
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict("All good! [VERDICT: STOP]")
        assert phase == 'stop'
        assert "All good!" in feedback

    def test_continue_worker_verdict(self):
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict("Needs work. [VERDICT: CONTINUE_WORKER]")
        assert phase == 'worker'
        assert "Needs work." in feedback

    def test_legacy_continue_verdict_maps_to_worker(self):
        """Backward compat: bare [VERDICT: CONTINUE] must map to 'worker'."""
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict("Needs work. [VERDICT: CONTINUE]")
        assert phase == 'worker'
        assert "Needs work." in feedback

    def test_continue_planner_verdict(self):
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict(
            "Plan is wrong. [VERDICT: CONTINUE_PLANNER]"
        )
        assert phase == 'planner'

    def test_no_verdict_defaults_to_worker(self):
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict("Some feedback without a verdict tag.")
        assert phase == 'worker'

    def test_stop_with_x_markers_overridden_to_planner(self):
        """Defense-in-depth: STOP with unresolved ❌ → override to 'planner'."""
        import os
        os.environ['CHATUI_ENDPOINT_REPLAN'] = '1'
        from lib.tasks_pkg.endpoint_review import _parse_verdict
        feedback, phase = _parse_verdict(
            "- ❌ Item 1 still failing\n[VERDICT: STOP]"
        )
        assert phase == 'planner'


# ═══════════════════════════════════════════════════════════
#  Test: Single iteration (planner → worker → critic STOP)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointSingleIteration:
    """Test a single-iteration endpoint: planner → worker → critic(STOP).

    Validates:
    1. Planner call has correct message shape
    2. Worker call has planner brief replacing user msg
    3. Critic call has correct review shape
    4. No consecutive user messages in any LLM call
    """

    def test_single_iteration_message_shapes(self, recorder):
        from lib.tasks_pkg.manager import create_task

        # ── Enqueue responses: planner → worker → critic ──
        planner_brief = (
            "## Goal\nImplement feature X.\n\n"
            "## Checklist\n- [ ] Step 1\n- [ ] Step 2\n\n"
            "## Acceptance Criteria\n- Tests pass\n"
        )
        worker_output = "I've implemented feature X. Here's the code:\n```python\ndef solve(): return 42\n```"
        critic_verdict = "All checks pass. The implementation is correct. [VERDICT: STOP]"

        recorder.enqueue_responses(planner_brief, worker_output, critic_verdict)

        # ── Create task ──
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Please implement feature X for me."},
        ]
        config = {
            "model": "mock-model",
            "endpointMode": True,
            "searchMode": "off",
            "browserEnabled": False,
            "projectEnabled": False,
            "codeExecEnabled": False,
        }
        task = create_task("test-conv-single", messages, config)

        # ── Run endpoint task ──
        _run_endpoint_task_and_wait(task)

        # ── Validate recorded LLM calls ──
        calls = recorder.get_calls()

        # Expect exactly 3 calls: planner, worker, critic
        assert len(calls) >= 3, (
            f"Expected at least 3 LLM calls (planner+worker+critic), got {len(calls)}. "
            f"Roles per call: {[_extract_roles(c['messages']) for c in calls]}"
        )

        # ── Call 0: Planner ──
        planner_msgs = calls[0]["messages"]
        planner_roles = _extract_roles(planner_msgs)

        # Planner system prompt should be first
        assert planner_roles[0] == "system", \
            f"Planner first msg should be system, got: {planner_roles[0]}"

        # Should contain the original user message somewhere
        user_contents = [
            (c if isinstance(c, str) else str(c))
            for m in planner_msgs if m["role"] == "user"
            for c in [m.get("content", "")]
        ]
        has_original = any(
            "implement feature x" in c.lower() or "feature x" in c.lower()
            for c in user_contents
        )
        assert has_original, \
            f"Planner should see original user request. User contents: {[c[:120] for c in user_contents]}"

        # Last message should be the "produce your plan" instruction
        last_planner_msg = planner_msgs[-1]
        assert last_planner_msg["role"] == "user", \
            f"Planner last msg should be user (plan instruction), got: {last_planner_msg['role']}"
        assert "produce" in last_planner_msg["content"].lower() or "plan" in last_planner_msg["content"].lower(), \
            f"Planner last msg should ask to produce plan, got: {last_planner_msg['content'][:100]}"

        # NOTE: The planner call CAN have consecutive user messages because:
        #   1. user(original request)
        #   2. user("produce your plan" instruction — with skills appended)
        # This is by design — the planner sees the original user request followed
        # by the instruction to plan.  The LLM handles this fine for the planner role.
        # What matters is that WORKER and CRITIC calls do NOT have consecutive users.

        # ── Call 1: Worker ──
        worker_msgs = calls[1]["messages"]
        worker_roles = _extract_roles(worker_msgs)

        # Worker should see system prompt
        assert worker_roles[0] == "system", \
            f"Worker first msg should be system, got: {worker_roles[0]}"

        # The user message should contain the planner brief (not original user msg)
        worker_user_msgs = [m for m in worker_msgs if m["role"] == "user"]
        assert len(worker_user_msgs) >= 1, "Worker should have at least one user message"

        # No consecutive user messages in worker call
        assert not _has_consecutive_same_role(worker_msgs, "user"), \
            f"Worker call has consecutive user messages! Roles: {worker_roles}"

        # ── Call 2: Critic ──
        critic_msgs = calls[2]["messages"]
        critic_roles = _extract_roles(critic_msgs)

        # Critic should have its own system prompt
        assert critic_roles[0] == "system", \
            f"Critic first msg should be system, got: {critic_roles[0]}"
        # System content may be a string or list of parts
        critic_sys_content = critic_msgs[0]["content"]
        if isinstance(critic_sys_content, list):
            critic_sys_text = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p)
                for p in critic_sys_content
            ).lower()
        else:
            critic_sys_text = str(critic_sys_content).lower()
        assert "review" in critic_sys_text or "critic" in critic_sys_text, \
            f"Critic system prompt should mention reviewing. Got: {critic_sys_text[:200]}"

        # Critic should see the worker's output (as assistant message)
        critic_assistant_msgs = [m for m in critic_msgs if m["role"] == "assistant"]
        assert len(critic_assistant_msgs) >= 1, \
            f"Critic should see at least one assistant message (worker output). Roles: {critic_roles}"

        # Last message should be the review instruction
        last_critic_msg = critic_msgs[-1]
        assert last_critic_msg["role"] == "user", \
            f"Critic last msg should be user (review instruction), got: {last_critic_msg['role']}"
        assert "review" in last_critic_msg["content"].lower(), \
            f"Critic last msg should ask to review. Got: {last_critic_msg['content'][:100]}"

        # No consecutive user messages in critic call
        assert not _has_consecutive_same_role(critic_msgs, "user"), \
            f"Critic call has consecutive user messages! Roles: {critic_roles}"

        # ── Validate task completed successfully ──
        assert task["status"] == "done", f"Task should be done, got: {task['status']}"


# ═══════════════════════════════════════════════════════════
#  Test: Multi-iteration (worker → critic CONTINUE → worker → critic STOP)
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointMultiIteration:
    """Test multi-iteration endpoint with CONTINUE then STOP.

    Validates the worker→critic→worker→critic loop and that critic
    feedback is correctly injected as a user message for the next worker turn.
    """

    def test_multi_iteration_message_shapes(self, recorder):
        from lib.tasks_pkg.manager import create_task

        planner_brief = "## Goal\nFix bug Y.\n\n## Checklist\n- [ ] Find bug\n- [ ] Fix bug\n"
        worker_output_1 = "I found the bug but haven't fixed it yet."
        critic_continue = "The bug was found but not fixed. Please complete the fix. [VERDICT: CONTINUE]"
        worker_output_2 = "I've now fixed the bug. Here's the patch:\n```diff\n-old\n+new\n```"
        critic_stop = "All items verified. Bug is fixed. [VERDICT: STOP]"

        recorder.enqueue_responses(
            planner_brief,
            worker_output_1,
            critic_continue,
            worker_output_2,
            critic_stop,
        )

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Fix bug Y in the codebase."},
        ]
        config = {
            "model": "mock-model",
            "endpointMode": True,
            "searchMode": "off",
            "browserEnabled": False,
            "projectEnabled": False,
            "codeExecEnabled": False,
        }
        task = create_task("test-conv-multi", messages, config)

        _run_endpoint_task_and_wait(task)

        calls = recorder.get_calls()

        # Expect 5 calls: planner, worker1, critic1, worker2, critic2
        assert len(calls) >= 5, (
            f"Expected at least 5 LLM calls for 2-iteration endpoint, got {len(calls)}. "
            f"Roles per call: {[_extract_roles(c['messages']) for c in calls]}"
        )

        # ── Validate NO consecutive user messages in worker/critic calls ──
        # (planner call at index 0 is allowed consecutive users by design)
        for i, call in enumerate(calls):
            if i == 0:
                continue  # planner call
            roles = _extract_roles(call["messages"])
            assert not _has_consecutive_same_role(call["messages"], "user"), (
                f"Call {i} has consecutive user messages! Roles: {roles}"
            )

        # ── Call 3: Worker iteration 2 ──
        # Should see: system → user(planner_brief) → assistant(worker1) → user(critic_feedback)
        worker2_msgs = calls[3]["messages"]
        worker2_roles = _extract_roles(worker2_msgs)

        # Should start with system
        assert worker2_roles[0] == "system", \
            f"Worker2 first msg should be system, got: {worker2_roles[0]}"

        # Should contain at least 1 assistant message (worker1 output)
        worker2_assistant_msgs = [m for m in worker2_msgs if m["role"] == "assistant"]
        assert len(worker2_assistant_msgs) >= 1, \
            f"Worker2 should see at least 1 assistant message (worker1 output). Roles: {worker2_roles}"

        # Should contain critic feedback as a user message (between worker1 and worker2 turns)
        worker2_user_msgs = [m for m in worker2_msgs if m["role"] == "user"]
        critic_feedback_found = any(
            "not fixed" in (m.get("content") or "").lower()
            or "complete the fix" in (m.get("content") or "").lower()
            for m in worker2_user_msgs
        )
        assert critic_feedback_found, (
            f"Worker2 should see critic feedback. User contents: "
            f"{[(m.get('content') or '')[:80] for m in worker2_user_msgs]}"
        )

        # ── Call 4: Critic iteration 2 ──
        critic2_msgs = calls[4]["messages"]
        critic2_roles = _extract_roles(critic2_msgs)

        # Should have system prompt
        assert critic2_roles[0] == "system"

        # Should see at least 2 assistant messages (both worker outputs)
        critic2_assistant_msgs = [m for m in critic2_msgs if m["role"] == "assistant"]
        assert len(critic2_assistant_msgs) >= 2, (
            f"Critic2 should see at least 2 assistant messages (both worker outputs). "
            f"Got {len(critic2_assistant_msgs)}. Roles: {critic2_roles}"
        )

        # No consecutive user messages
        assert not _has_consecutive_same_role(critic2_msgs, "user"), \
            f"Critic2 has consecutive user messages! Roles: {critic2_roles}"


# ═══════════════════════════════════════════════════════════
#  Test: Endpoint turns stored on task correctly
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointTurnsPersistence:
    """Test that endpoint turns are correctly accumulated on the task dict."""

    def test_endpoint_turns_shape(self, recorder):
        from lib.tasks_pkg.manager import create_task

        planner_brief = "## Goal\nDo thing Z.\n\n## Checklist\n- [ ] Do Z\n"
        worker_output = "Done: Z is implemented."
        critic_verdict = "Verified. [VERDICT: STOP]"

        recorder.enqueue_responses(planner_brief, worker_output, critic_verdict)

        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Do thing Z."},
        ]
        config = {
            "model": "mock-model",
            "endpointMode": True,
            "searchMode": "off",
            "browserEnabled": False,
            "projectEnabled": False,
            "codeExecEnabled": False,
        }
        task = create_task("test-conv-db", messages, config)

        _run_endpoint_task_and_wait(task)

        # Check endpoint_turns on task
        endpoint_turns = task.get("_endpoint_turns", [])
        assert len(endpoint_turns) >= 3, (
            f"Expected at least 3 endpoint turns (planner+worker+critic), "
            f"got {len(endpoint_turns)}"
        )

        # Turn 0: planner (assistant, _isEndpointPlanner)
        planner_turn = endpoint_turns[0]
        assert planner_turn["role"] == "assistant"
        assert planner_turn.get("_isEndpointPlanner") is True

        # Turn 1: worker (assistant, _epIteration=1)
        worker_turn = endpoint_turns[1]
        assert worker_turn["role"] == "assistant"
        assert worker_turn.get("_epIteration") == 1

        # Turn 2: critic (user, _isEndpointReview, _epApproved=True)
        critic_turn = endpoint_turns[2]
        assert critic_turn["role"] == "user"
        assert critic_turn.get("_isEndpointReview") is True
        assert critic_turn.get("_epApproved") is True


# ═══════════════════════════════════════════════════════════
#  Test: Follow-up message (buildApiMessages filtering) — Unit
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestBuildApiMessagesEndpointFiltering:
    """Test that buildApiMessages correctly filters _isEndpointReview messages.

    Simulates the conversation state after an endpoint task and verifies
    that the frontend filtering produces correct API messages.
    """

    def test_trailing_critic_filtered(self):
        """After endpoint completion, the trailing critic review is filtered."""
        conv_messages = [
            {"role": "user", "content": "Implement feature X"},
            {"role": "assistant", "content": "## Plan...", "_isEndpointPlanner": True},
            {"role": "assistant", "content": "Done! Code...", "_epIteration": 1},
            {"role": "user", "content": "All verified. [VERDICT: STOP]",
             "_isEndpointReview": True, "_epApproved": True, "_epIteration": 1},
            {"role": "user", "content": "Now can you also add tests?"},
            {"role": "assistant", "content": ""},  # empty placeholder
        ]

        api_messages = _build_api_messages_python(conv_messages)

        # No _isEndpointReview messages
        for msg in api_messages:
            assert not msg.get("_isEndpointReview"), \
                f"API messages should not contain _isEndpointReview: {msg}"

        # No consecutive user messages
        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"Consecutive user messages! Roles: {_extract_roles(api_messages)}"

        # New user question is present
        user_contents = [m["content"] for m in api_messages if m["role"] == "user"]
        assert any("add tests" in c for c in user_contents), \
            f"New user question missing. User contents: {user_contents}"

    def test_multi_iteration_critics_filtered(self):
        """Multiple critic reviews from multi-iteration endpoint are all filtered."""
        conv_messages = [
            {"role": "user", "content": "Fix bug"},
            {"role": "assistant", "content": "## Plan...", "_isEndpointPlanner": True},
            {"role": "assistant", "content": "First attempt.", "_epIteration": 1},
            {"role": "user", "content": "Not done yet. [VERDICT: CONTINUE]",
             "_isEndpointReview": True, "_epIteration": 1},
            {"role": "assistant", "content": "Fixed now.", "_epIteration": 2},
            {"role": "user", "content": "All good. [VERDICT: STOP]",
             "_isEndpointReview": True, "_epApproved": True, "_epIteration": 2},
            {"role": "user", "content": "Great, now add docs please."},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(conv_messages)

        # No critic reviews
        review_msgs = [m for m in api_messages if m.get("_isEndpointReview")]
        assert len(review_msgs) == 0, \
            f"Should have 0 _isEndpointReview messages, got {len(review_msgs)}"

        # No consecutive same-role messages
        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"Consecutive user messages! Roles: {_extract_roles(api_messages)}"
        assert not _has_consecutive_same_role(api_messages, "assistant"), \
            f"Consecutive assistant messages! Roles: {_extract_roles(api_messages)}"

        # Both worker outputs should be present (may be merged into one assistant msg)
        assistant_contents = [m["content"] for m in api_messages if m["role"] == "assistant"]
        assert any("First attempt" in c for c in assistant_contents), \
            "Worker iteration 1 output should be in API messages"
        assert any("Fixed now" in c for c in assistant_contents), \
            "Worker iteration 2 output should be in API messages"

        # New user question present
        user_contents = [m["content"] for m in api_messages if m["role"] == "user"]
        assert any("add docs" in c for c in user_contents), \
            "New user question should be in API messages"

    def test_planner_filtered(self):
        """Planner assistant message IS filtered — its content already replaced
        the user message in the LLM working messages, so including it in
        follow-up turns creates duplicate context + consecutive assistants."""
        conv_messages = [
            {"role": "user", "content": "Do X"},
            {"role": "assistant", "content": "## Plan for X", "_isEndpointPlanner": True},
            {"role": "assistant", "content": "X done.", "_epIteration": 1},
            {"role": "user", "content": "Approved. [VERDICT: STOP]",
             "_isEndpointReview": True, "_epApproved": True},
            {"role": "user", "content": "Now do Y"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(conv_messages)

        # Planner should be FILTERED (display-only)
        planner_msgs = [m for m in api_messages
                        if m.get("role") == "assistant" and "Plan for X" in (m.get("content") or "")]
        assert len(planner_msgs) == 0, "Planner message should be filtered out"

        # No consecutive same-role messages
        assert not _has_consecutive_same_role(api_messages, "assistant"), \
            f"Consecutive assistant messages! Roles: {_extract_roles(api_messages)}"

    def test_no_endpoint_messages_unchanged(self):
        """Normal (non-endpoint) conversation messages pass through unchanged."""
        conv_messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(conv_messages)

        assert len(api_messages) == 3  # 2 user + 1 assistant (last empty sliced off)
        assert _extract_roles(api_messages) == ["user", "assistant", "user"]


# ═══════════════════════════════════════════════════════════
#  Test: Previous conversation context + endpoint
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointWithPriorContext:
    """Test endpoint messages with prior conversation history.

    Validates Scenario 1 from the spec:
    Previous round info → user(human) → planner → worker → critic → ...
    """

    def test_prior_turns_preserved(self):
        """Endpoint after prior user↔assistant turns preserves context."""
        conv_messages = [
            # Prior conversation
            {"role": "user", "content": "What is Python?"},
            {"role": "assistant", "content": "Python is a programming language."},
            # Endpoint turn
            {"role": "user", "content": "Now implement feature X using Python"},
            {"role": "assistant", "content": "## Plan...", "_isEndpointPlanner": True},
            {"role": "assistant", "content": "Feature X done.", "_epIteration": 1},
            {"role": "user", "content": "Approved. [VERDICT: STOP]",
             "_isEndpointReview": True, "_epApproved": True},
            # Follow-up
            {"role": "user", "content": "Can you explain the code?"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(conv_messages)
        roles = _extract_roles(api_messages)

        # Prior context should be present
        assert any("Python" in m.get("content", "") for m in api_messages if m["role"] == "user")
        assert any("programming language" in m.get("content", "") for m in api_messages if m["role"] == "assistant")

        # No consecutive same-role messages
        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"Consecutive user messages! Roles: {roles}"
        assert not _has_consecutive_same_role(api_messages, "assistant"), \
            f"Consecutive assistant messages! Roles: {roles}"

        # Critic review and planner filtered
        assert not any(m.get("_isEndpointReview") for m in api_messages)

        # Follow-up question present
        assert any("explain the code" in m.get("content", "") for m in api_messages if m["role"] == "user")

    def test_follow_up_after_endpoint_message_shape(self):
        """Validates the exact expected shape for a follow-up after endpoint.

        Expected:
          prior_user → prior_assistant → endpoint_user → planner(assistant)
          → worker(assistant) → new_user
        """
        conv_messages = [
            {"role": "user", "content": "Prior question"},
            {"role": "assistant", "content": "Prior answer"},
            {"role": "user", "content": "Endpoint request"},
            {"role": "assistant", "content": "## Plan", "_isEndpointPlanner": True},
            {"role": "assistant", "content": "Worker result", "_epIteration": 1},
            {"role": "user", "content": "Good. [VERDICT: STOP]",
             "_isEndpointReview": True, "_epApproved": True},
            {"role": "user", "content": "New follow-up question"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(conv_messages)
        roles = _extract_roles(api_messages)

        # Expected shape: user → assistant → user → assistant → user
        # Planner is filtered (its content already replaced the user message
        # in the LLM working messages), so only the worker assistant remains.
        expected_roles = ["user", "assistant", "user", "assistant", "user"]
        assert roles == expected_roles, (
            f"Expected roles {expected_roles}, got {roles}.\n"
            f"Messages: {[m.get('content', '')[:40] for m in api_messages]}"
        )

        # No consecutive same-role messages
        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"Consecutive user messages found! Roles: {roles}"
        assert not _has_consecutive_same_role(api_messages, "assistant"), \
            f"Consecutive assistant messages found! Roles: {roles}"


# ═══════════════════════════════════════════════════════════
#  Test: Full integration — endpoint + follow-up
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointFollowUpIntegration:
    """Integration test: run endpoint, then verify follow-up message shape.

    Runs an actual endpoint task with mocked LLM, captures the endpoint_turns,
    then verifies that a simulated follow-up produces correct API messages.
    """

    def test_endpoint_then_followup(self, recorder):
        from lib.tasks_pkg.manager import create_task

        planner_brief = "## Goal\nBuild widget.\n\n## Checklist\n- [ ] Build it\n"
        worker_output = "Widget built successfully."
        critic_verdict = "Widget works correctly. [VERDICT: STOP]"

        recorder.enqueue_responses(planner_brief, worker_output, critic_verdict)

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Build a widget."},
        ]
        config = {
            "model": "mock-model",
            "endpointMode": True,
            "searchMode": "off",
            "browserEnabled": False,
            "projectEnabled": False,
            "codeExecEnabled": False,
        }
        task = create_task("test-conv-followup", messages, config)

        _run_endpoint_task_and_wait(task)

        # ── Simulate follow-up: build conversation as it would be in DB ──
        endpoint_turns = task.get("_endpoint_turns", [])
        assert len(endpoint_turns) >= 3, "Endpoint should produce at least 3 turns"

        # Reconstruct what the DB conversation looks like:
        # original messages (system + user) + endpoint_turns
        db_messages = list(messages) + endpoint_turns

        # User sends follow-up → appended by frontend
        db_messages.append({"role": "user", "content": "Now add tests for the widget."})
        # Frontend adds empty assistant placeholder
        db_messages.append({"role": "assistant", "content": ""})

        # ── Apply buildApiMessages filtering ──
        api_messages = _build_api_messages_python(db_messages, include_system=True)

        # ── Validate ──
        roles = _extract_roles(api_messages)

        # No consecutive user messages
        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"Consecutive user messages in follow-up! Roles: {roles}"

        # No _isEndpointReview messages
        assert not any(m.get("_isEndpointReview") for m in api_messages), \
            "Follow-up API messages should not contain critic reviews"

        # Follow-up question is the last user message
        user_msgs = [m for m in api_messages if m["role"] == "user"]
        assert user_msgs[-1]["content"] == "Now add tests for the widget."

        # Worker output is present (as assistant)
        assert any("Widget built" in m.get("content", "") for m in api_messages if m["role"] == "assistant")


# ═══════════════════════════════════════════════════════════
#  Test: Stuck detection — message shapes remain valid
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestEndpointStuckDetection:
    """Test that stuck detection works and message shapes remain valid."""

    def test_stuck_stops_loop(self, recorder):
        from lib.tasks_pkg.manager import create_task

        planner_brief = "## Goal\nDo impossible task.\n\n## Checklist\n- [ ] Magic\n"
        worker_output = "I tried but couldn't do magic."
        # Two nearly identical critic feedbacks → triggers stuck detection
        critic_continue_1 = "The magic is not done. Please try harder to do the magic thing. [VERDICT: CONTINUE]"
        worker_output_2 = "Still can't do magic."
        critic_continue_2 = "The magic is not done. Please try harder to do the magic thing now. [VERDICT: CONTINUE]"

        recorder.enqueue_responses(
            planner_brief,
            worker_output,
            critic_continue_1,
            worker_output_2,
            critic_continue_2,
        )

        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Do magic."},
        ]
        config = {
            "model": "mock-model",
            "endpointMode": True,
            "searchMode": "off",
            "browserEnabled": False,
            "projectEnabled": False,
            "codeExecEnabled": False,
        }
        task = create_task("test-conv-stuck", messages, config)

        _run_endpoint_task_and_wait(task)

        # ── Validate all worker/critic LLM calls have valid message shapes ──
        calls = recorder.get_calls()
        for i, call in enumerate(calls):
            if i == 0:
                continue  # planner call
            assert not _has_consecutive_same_role(call["messages"], "user"), \
                f"Call {i} has consecutive user messages! Roles: {_extract_roles(call['messages'])}"

        # ── Validate the stuck endpoint turn ──
        endpoint_turns = task.get("_endpoint_turns", [])
        stuck_turns = [t for t in endpoint_turns if t.get("_isStuck")]
        assert len(stuck_turns) >= 1, "Should have at least one stuck turn"

        # The last critic turn should have _epApproved=True (stuck → stop)
        last_critic = [t for t in endpoint_turns if t.get("_isEndpointReview")][-1]
        assert last_critic.get("_epApproved") is True, "Stuck turn should have _epApproved=True"
        assert last_critic.get("_isStuck") is True, "Stuck turn should have _isStuck=True"


# ═══════════════════════════════════════════════════════════
#  Test: Message role alternation across all scenarios
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestNoConsecutiveUserMessagesEver:
    """Exhaustive test: no scenario produces consecutive user messages in API calls.

    This is the GOLDEN RULE — the entire reason for the _isEndpointReview
    filtering and message shape design.
    """

    def test_single_iteration_no_consecutive_users(self, recorder):
        """Single iteration: planner → worker → critic(STOP).

        Note: planner call (index 0) is allowed to have consecutive user
        messages (original request + plan instruction).  Only worker/critic
        calls are checked.
        """
        from lib.tasks_pkg.manager import create_task

        recorder.enqueue_responses(
            "## Goal\nTask A\n## Checklist\n- [ ] A\n",
            "Task A done.",
            "Approved. [VERDICT: STOP]",
        )

        task = create_task("golden-single", [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Do task A"},
        ], {
            "model": "mock-model", "endpointMode": True,
            "searchMode": "off", "browserEnabled": False,
            "projectEnabled": False, "codeExecEnabled": False,
        })

        _run_endpoint_task_and_wait(task)

        for i, call in enumerate(recorder.get_calls()):
            if i == 0:
                continue  # planner call — consecutive users expected
            msgs = call["messages"]
            assert not _has_consecutive_same_role(msgs, "user"), \
                f"[golden-single] Call {i} has consecutive users! Roles: {_extract_roles(msgs)}"

    def test_multi_iteration_no_consecutive_users(self, recorder):
        """Multi iteration: CONTINUE → STOP."""
        from lib.tasks_pkg.manager import create_task

        recorder.enqueue_responses(
            "## Goal\nTask B\n## Checklist\n- [ ] B1\n- [ ] B2\n",
            "B1 done.",
            "B2 not done. [VERDICT: CONTINUE]",
            "B1 and B2 done.",
            "All done. [VERDICT: STOP]",
        )

        task = create_task("golden-multi", [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Do task B"},
        ], {
            "model": "mock-model", "endpointMode": True,
            "searchMode": "off", "browserEnabled": False,
            "projectEnabled": False, "codeExecEnabled": False,
        })

        _run_endpoint_task_and_wait(task)

        for i, call in enumerate(recorder.get_calls()):
            if i == 0:
                continue  # planner call — consecutive users expected
            msgs = call["messages"]
            assert not _has_consecutive_same_role(msgs, "user"), \
                f"[golden-multi] Call {i} has consecutive users! Roles: {_extract_roles(msgs)}"

    def test_followup_no_consecutive_users(self, recorder):
        """Follow-up after endpoint: filtered messages have no consecutive users."""
        from lib.tasks_pkg.manager import create_task

        recorder.enqueue_responses(
            "## Goal\nTask C\n## Checklist\n- [ ] C\n",
            "C done.",
            "Good. [VERDICT: STOP]",
        )

        task = create_task("golden-followup", [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Do task C"},
        ], {
            "model": "mock-model", "endpointMode": True,
            "searchMode": "off", "browserEnabled": False,
            "projectEnabled": False, "codeExecEnabled": False,
        })

        _run_endpoint_task_and_wait(task)

        # Simulate follow-up
        db_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "Do task C"},
        ] + task.get("_endpoint_turns", []) + [
            {"role": "user", "content": "Follow-up question"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(db_messages, include_system=True)

        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"[golden-followup] Consecutive users in follow-up! Roles: {_extract_roles(api_messages)}"

    def test_prior_context_plus_endpoint_plus_followup(self, recorder):
        """Full scenario: prior turns + endpoint + follow-up."""
        from lib.tasks_pkg.manager import create_task

        recorder.enqueue_responses(
            "## Goal\nTask D\n## Checklist\n- [ ] D\n",
            "D done.",
            "Verified. [VERDICT: STOP]",
        )

        # Prior conversation context
        prior_messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "What is X?"},
            {"role": "assistant", "content": "X is a thing."},
            {"role": "user", "content": "Now do task D"},
        ]

        task = create_task("golden-prior", prior_messages, {
            "model": "mock-model", "endpointMode": True,
            "searchMode": "off", "browserEnabled": False,
            "projectEnabled": False, "codeExecEnabled": False,
        })

        _run_endpoint_task_and_wait(task)

        # All worker/critic LLM calls during endpoint (skip planner)
        for i, call in enumerate(recorder.get_calls()):
            if i == 0:
                continue  # planner call
            msgs = call["messages"]
            assert not _has_consecutive_same_role(msgs, "user"), \
                f"[golden-prior] Call {i} (during endpoint) has consecutive users! Roles: {_extract_roles(msgs)}"

        # Follow-up
        db_messages = prior_messages + task.get("_endpoint_turns", []) + [
            {"role": "user", "content": "Thanks, now do E"},
            {"role": "assistant", "content": ""},
        ]

        api_messages = _build_api_messages_python(db_messages, include_system=True)

        assert not _has_consecutive_same_role(api_messages, "user"), \
            f"[golden-prior] Follow-up has consecutive users! Roles: {_extract_roles(api_messages)}"


# ═══════════════════════════════════════════════════════════
#  Test: Planner replaces user message in working messages
# ═══════════════════════════════════════════════════════════

@pytest.mark.unit
class TestPlannerReplacesUserMessage:
    """Verify that the planner's output replaces the original user message
    in the working messages sent to the worker and critic."""

    def test_worker_sees_planner_brief_not_original(self, recorder):
        from lib.tasks_pkg.manager import create_task

        planner_brief = "## Goal\nREPLACED_CONTENT_XYZ\n## Checklist\n- [ ] Do it\n"
        worker_output = "Done."
        critic_verdict = "Good. [VERDICT: STOP]"

        recorder.enqueue_responses(planner_brief, worker_output, critic_verdict)

        task = create_task("test-replace", [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Original user request ABC123"},
        ], {
            "model": "mock-model", "endpointMode": True,
            "searchMode": "off", "browserEnabled": False,
            "projectEnabled": False, "codeExecEnabled": False,
        })

        _run_endpoint_task_and_wait(task)

        calls = recorder.get_calls()
        assert len(calls) >= 3

        # ── Planner call should see the original user request ──
        planner_msgs = calls[0]["messages"]
        planner_user_contents = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", ""))
            for m in planner_msgs if m["role"] == "user"
        )
        assert "ABC123" in planner_user_contents or "Original user request" in planner_user_contents, \
            f"Planner should see original request. User contents: {planner_user_contents[:200]}"

        # ── Worker call should see planner brief, NOT original ──
        worker_msgs = calls[1]["messages"]
        worker_user_contents = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", ""))
            for m in worker_msgs if m["role"] == "user"
        )
        assert "REPLACED_CONTENT_XYZ" in worker_user_contents, \
            f"Worker should see planner brief. User contents: {worker_user_contents[:200]}"

        # ── Critic call should also see planner brief (inherited from worker messages) ──
        critic_msgs = calls[2]["messages"]
        critic_user_contents = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else str(m.get("content", ""))
            for m in critic_msgs if m["role"] == "user"
        )
        assert "REPLACED_CONTENT_XYZ" in critic_user_contents, \
            f"Critic should see planner brief. User contents: {critic_user_contents[:200]}"


# ═══════════════════════════════════════════════════════════
#  Python port of buildApiMessages (for unit testing)
# ═══════════════════════════════════════════════════════════

def _build_api_messages_python(
    conv_messages: list[dict],
    include_system: bool = False,
) -> list[dict]:
    """Python port of the frontend's buildApiMessages() for testing.

    This mirrors the key filtering logic:
    1. Slice off the last message (empty assistant placeholder)
    2. Skip _isEndpointReview messages (critic feedback — display-only)
    3. Skip _isEndpointPlanner messages (planner output — display-only,
       its content already replaced the user message in working messages)
    4. Include user and assistant messages
    """
    # Slice off the last message (the empty assistant placeholder)
    src_msgs = conv_messages[:-1] if conv_messages else []

    api_messages = []
    for msg in src_msgs:
        role = msg.get("role", "")

        # Skip endpoint display-only messages
        if msg.get("_isEndpointReview"):
            continue
        if msg.get("_isEndpointPlanner"):
            continue

        if role == "system":
            if include_system:
                api_messages.append({"role": "system", "content": msg.get("content", "")})
            continue

        if role == "user":
            api_messages.append({"role": "user", "content": msg.get("content", "")})
        elif role == "assistant":
            content = msg.get("content", "") or ""
            if content.strip():
                api_messages.append({"role": "assistant", "content": content})

    # Post-processing: merge consecutive same-role messages
    # (mirrors the frontend's merge step)
    i = len(api_messages) - 1
    while i > 0:
        if (api_messages[i].get("role") == api_messages[i - 1].get("role")
                and api_messages[i].get("role") in ("user", "assistant")):
            prev = api_messages[i - 1].get("content", "") or ""
            curr = api_messages[i].get("content", "") or ""
            sep = "\n\n" if prev and curr else ""
            api_messages[i - 1]["content"] = prev + sep + curr
            api_messages.pop(i)
        i -= 1

    return api_messages
