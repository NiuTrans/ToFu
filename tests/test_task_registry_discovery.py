"""tests/test_task_registry_discovery.py — P6: /api/v1/tasks kind discovery.

``routes/api_v1/tasks.py::_registries()`` is what the generic task endpoints
(``/api/v1/tasks``, ``/{id}``, ``/{id}/events``, ``/{id}/stream``,
``/{id}/abort``) enumerate. It was a HARD-CODED list of four in-tree runtimes,
so two shipped capabilities — motion-video and paper-podcast — were invisible
to every generic endpoint even though both are ordinary ``TaskRuntime``
instances with the standard task shape. That is the concrete cost the design
production map (docs/modules/production.md) names: podcast had to hand-write
its own ``poll_podcast_task`` because the generic poll could not see it.

These tests pin the discovery contract:
  * both capabilities are enumerated (this FAILED before the fix);
  * discovery is keyed on each runtime's OWN ``.kind``, not a literal in the
    route file, so a renamed kind can't silently desync;
  * every discovered runtime satisfies the interface the generic endpoints
    actually call — a registry entry that 500s on ``/events`` would be worse
    than being absent;
  * an unimportable entry degrades to "absent", never an exception (the
    endpoints must keep serving the runtimes that DID import).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def _registries():
    from routes.api_v1.tasks import _registries as reg
    return reg()


def test_motion_and_podcast_are_discovered():
    """Both shipped capabilities must appear — the P6 discovery fix."""
    kinds = set(_registries())
    assert 'motion-video' in kinds, (
        'motion-video runtime not discovered — /api/v1/tasks cannot see '
        'motion jobs')
    assert 'paper-podcast' in kinds, (
        'paper-podcast runtime not discovered — the reason podcast had to '
        'hand-write its own poll route')


def test_core_kinds_still_present():
    """The pre-existing kinds must not regress while adding new ones."""
    kinds = set(_registries())
    assert 'chat' in kinds
    # paper-report / translate come from routes.paper / routes.translate.
    assert any(k.startswith('paper') for k in kinds), sorted(kinds)


def test_keys_match_each_runtime_own_kind():
    """The dict key must BE the runtime's .kind.

    If the route file hard-coded a string instead, renaming a runtime's kind
    would silently desync the key from what /api/v1/tasks?kind=… filters on.
    """
    for key, rt in _registries().items():
        assert key == rt.kind, f'key {key!r} != runtime.kind {rt.kind!r}'


def test_every_discovered_runtime_satisfies_the_endpoint_interface():
    """Discovery must not add a runtime the generic endpoints would crash on.

    list_tasks touches ``_lock`` / ``_tasks``; the others call get / poll /
    abort. A half-shaped registry entry is worse than an absent one.
    """
    for kind, rt in _registries().items():
        for attr in ('_lock', '_tasks', 'get', 'poll', 'abort', 'kind'):
            assert hasattr(rt, attr), f'{kind} runtime lacks {attr}'


def test_unimportable_source_degrades_to_absent(monkeypatch):
    """A capability whose module fails to import must be SKIPPED, not fatal —
    the endpoints keep serving whatever else resolved."""
    import builtins
    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name == 'lib.motion_video.runtime':
            raise ImportError('simulated missing capability')
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, '__import__', boom)
    import sys
    monkeypatch.delitem(sys.modules, 'lib.motion_video.runtime', raising=False)
    kinds = set(_registries())          # must not raise
    assert 'motion-video' not in kinds
    assert 'chat' in kinds              # the rest still resolve
