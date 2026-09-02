"""tests/test_capabilities_agents_drift.py — Drift guard for the
hand-maintained ``_agents_summary()`` literal in
``routes/api_v1/capabilities.py``.

``_agents_summary()`` advertises agent endpoints (path + required scope) to
headless clients for auto-config. It is a hand-maintained literal, so it
historically drifted: it kept a dead ``swarm.run`` entry (no such route) and
missed genuinely-registered surfaces (``agent.run``, ``audio.*``, ``scheduler``,
``mcp``). These tests make that drift a hard failure:

  1. Every ``path`` in ``_agents_summary()`` must resolve to a route actually
     registered on the app's ``url_map`` (mounting every ``/api/v1`` blueprint).
  2. Every ``scope`` must be a member of the closed ``ALL_SCOPES`` enum.

The conftest installs the Flask→Quart shim at collection time, so importing
``routes.api_v1`` blueprints here is safe.
"""

import re
import unittest


def _build_app():
    """Register every /api/v1 blueprint so url_map has the full route set."""
    from quart import Quart
    from routes.api_v1 import ALL_V1_BLUEPRINTS

    app = Quart(__name__)
    app.config['TESTING'] = True
    for bp in ALL_V1_BLUEPRINTS:
        # Blueprints register on their own name; a repeat import in the same
        # process can raise on double-registration — guard defensively.
        try:
            app.register_blueprint(bp)
        except Exception:
            pass
    return app


_FLASK_VAR_RE = re.compile(r'<(?:[^:>]+:)?([^>]+)>')


def _registered_paths(app) -> set[str]:
    """All rule paths on the app, with Flask ``<var>`` normalised to ``{var}``.

    We compare on the ``{var}`` form so a parametrised advertised path could
    still match; today every advertised agent path is static.
    """
    out: set[str] = set()
    for rule in app.url_map.iter_rules():
        out.add(_FLASK_VAR_RE.sub(r'{\1}', str(rule.rule)))
    return out


class AgentsSummaryDriftTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _build_app()
        cls.paths = _registered_paths(cls.app)
        from routes.api_v1.capabilities import _agents_summary
        cls.entries = _agents_summary()

    def test_every_advertised_path_is_registered(self):
        """Each _agents_summary path must exist on the url_map."""
        missing = []
        for e in self.entries:
            p = _FLASK_VAR_RE.sub(r'{\1}', e['path'])
            if p not in self.paths:
                missing.append((e['id'], e['path']))
        self.assertEqual(
            missing, [],
            'These _agents_summary entries point at routes that are NOT '
            'registered on the app (drift — update the literal or the route): '
            + repr(missing))

    def test_every_advertised_scope_is_known(self):
        """Each _agents_summary scope must be in the closed ALL_SCOPES enum."""
        from lib.api_keys import ALL_SCOPES
        bad = [(e['id'], e['scope']) for e in self.entries
               if e['scope'] not in ALL_SCOPES]
        self.assertEqual(
            bad, [],
            'These _agents_summary entries use a scope not in ALL_SCOPES: '
            + repr(bad))

    def test_entries_have_required_shape(self):
        """Every entry carries id / path / scope strings."""
        for e in self.entries:
            self.assertIn('id', e)
            self.assertIn('path', e)
            self.assertIn('scope', e)
            self.assertTrue(e['path'].startswith('/api/v1/'), e['path'])

    def test_dead_swarm_run_entry_is_gone(self):
        """Regression: the phantom /api/v1/agents/swarm/run must not return."""
        ids = {e['id'] for e in self.entries}
        paths = {e['path'] for e in self.entries}
        self.assertNotIn('swarm.run', ids)
        self.assertNotIn('/api/v1/agents/swarm/run', paths)


if __name__ == '__main__':
    unittest.main()
