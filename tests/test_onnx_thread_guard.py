"""Unit tests for lib/onnx_thread_guard.py.

Root cause guarded here: onnxruntime creates ``InferenceSession`` objects with
``intra_op_num_threads == 0`` by default, which makes it spawn one worker per
HOST cpu and pin each via ``pthread_setaffinity_np``. On a cpuset-restricted
host the pins fail with EINVAL → a stderr storm during ``python server.py``.
The guard monkeypatches ``InferenceSession.__init__`` to inject an explicit
thread count so the affinity loop is skipped.

These tests inject a FAKE ``onnxruntime`` module (no real dep needed) that
records the ``sess_options`` its ``InferenceSession`` receives, so we can assert
the guard forces a non-zero thread count.

Run:  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 pytest tests/test_onnx_thread_guard.py -m unit
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_fake_ort():
    """Build a fake onnxruntime module with a recording InferenceSession."""
    ort = types.ModuleType('onnxruntime')

    class SessionOptions:
        def __init__(self):
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0

    class InferenceSession:
        # Records the sess_options every constructed session actually used.
        last_sess_options = None

        def __init__(self, *args, **kwargs):
            InferenceSession.last_sess_options = kwargs.get('sess_options')

    ort.SessionOptions = SessionOptions
    ort.InferenceSession = InferenceSession
    return ort


@pytest.fixture()
def fresh_guard(monkeypatch):
    """Reload the guard module with the module-level _installed flag reset and
    a fresh fake onnxruntime in sys.modules."""
    fake = _make_fake_ort()
    monkeypatch.setitem(sys.modules, 'onnxruntime', fake)
    import lib.onnx_thread_guard as guard
    monkeypatch.setattr(guard, '_installed', False)
    # Deterministic thread count regardless of host cpuset.
    monkeypatch.setenv('TOFU_ONNX_THREADS', '4')
    return guard, fake


@pytest.mark.unit
class TestOnnxThreadGuard:
    def test_default_session_gets_explicit_thread_cap(self, fresh_guard):
        """A session created with NO sess_options must come out with an
        explicit non-zero thread count (the whole point — 0 triggers the
        affinity storm)."""
        guard, fake = fresh_guard
        assert guard.install_onnx_thread_guard() is True
        # Construct a session the way pymupdf_layout does: no sess_options.
        fake.InferenceSession('model.onnx')
        so = fake.InferenceSession.last_sess_options
        assert so is not None, 'guard must inject a SessionOptions'
        assert so.intra_op_num_threads == 4
        assert so.inter_op_num_threads == 4

    def test_zero_valued_caller_options_are_capped(self, fresh_guard):
        """A caller passing SessionOptions with the default 0 must still get
        the cap (0 == 'spawn one per host cpu')."""
        guard, fake = fresh_guard
        guard.install_onnx_thread_guard()
        so_in = fake.SessionOptions()  # intra/inter == 0
        fake.InferenceSession('m.onnx', sess_options=so_in)
        assert so_in.intra_op_num_threads == 4
        assert so_in.inter_op_num_threads == 4

    def test_explicit_caller_cap_is_respected(self, fresh_guard):
        """A caller that already chose a non-zero cap is NOT overridden."""
        guard, fake = fresh_guard
        guard.install_onnx_thread_guard()
        so_in = fake.SessionOptions()
        so_in.intra_op_num_threads = 2
        so_in.inter_op_num_threads = 1
        fake.InferenceSession('m.onnx', sess_options=so_in)
        assert so_in.intra_op_num_threads == 2
        assert so_in.inter_op_num_threads == 1

    def test_idempotent_does_not_double_wrap(self, fresh_guard):
        """Calling install twice must not stack a second subclass layer."""
        guard, fake = fresh_guard
        assert guard.install_onnx_thread_guard() is True
        after_first = fake.InferenceSession
        assert guard.install_onnx_thread_guard() is True
        assert fake.InferenceSession is after_first

    def test_absent_onnxruntime_is_noop(self, monkeypatch):
        """When onnxruntime is not importable the guard returns False and
        never raises (minimal installs have no onnx)."""
        # Ensure any import of onnxruntime fails.
        monkeypatch.setitem(sys.modules, 'onnxruntime', None)
        import lib.onnx_thread_guard as guard
        monkeypatch.setattr(guard, '_installed', False)
        assert guard.install_onnx_thread_guard() is False

    def test_thread_count_env_precedence(self, monkeypatch):
        """TOFU_ONNX_THREADS wins; falls back to legacy TOFU_DOCLING_THREADS;
        else min(8, allowed_cpus)."""
        import lib.onnx_thread_guard as guard
        monkeypatch.setenv('TOFU_ONNX_THREADS', '3')
        monkeypatch.setenv('TOFU_DOCLING_THREADS', '9')
        assert guard.onnx_thread_count() == 3
        monkeypatch.delenv('TOFU_ONNX_THREADS', raising=False)
        assert guard.onnx_thread_count() == 9
        monkeypatch.delenv('TOFU_DOCLING_THREADS', raising=False)
        n = guard.onnx_thread_count()
        assert 1 <= n <= 8

    def test_NEUTER_without_guard_default_stays_zero(self, fresh_guard):
        """NEUTER: prove the cap is load-bearing. If we DON'T install the
        guard, a default-constructed session keeps intra_op_num_threads == 0
        — exactly the state that triggers the affinity storm. This confirms
        the guard (not some ambient default) is what fixes it."""
        guard, fake = fresh_guard
        # Deliberately do NOT call install_onnx_thread_guard().
        so_in = fake.SessionOptions()
        fake.InferenceSession('m.onnx', sess_options=so_in)
        assert so_in.intra_op_num_threads == 0
        assert so_in.inter_op_num_threads == 0
