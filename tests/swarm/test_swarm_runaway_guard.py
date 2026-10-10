"""Numeric round ceilings stay retired; shared invocation tests cover progress guards."""
import unittest
import pytest

pytestmark = pytest.mark.unit


class TestSubAgentRunawayGuard(unittest.TestCase):

    def test_removed_max_rounds_is_strictly_rejected(self):
        from lib.swarm.types import SubTaskSpec
        with self.assertRaises(TypeError):
            SubTaskSpec(role='researcher', objective='x', max_rounds=3)
        with self.assertRaises(ValueError):
            SubTaskSpec.from_dict({
                'role': 'researcher', 'objective': 'x', 'max_rounds': 3})

