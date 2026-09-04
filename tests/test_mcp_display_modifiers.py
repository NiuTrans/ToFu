"""MCP title operation chips — same-resource parallel calls must never
collapse into identical-looking rows.

Incident (conversation ``mtc9t5qiubhy2k``): one LLM round fired several
``hope/get_log_file`` calls against the SAME pod with different
``file_name``/``regex``/``method`` args. The old title composer only
rendered resource + container keys, so every sibling row showed the
byte-identical title ``hope/get_log_file — psxa9idx… @ cluster/ns`` and the
user read it as duplicate executions (they were not).

Fix in ``lib/tasks_pkg/tool_display/_mcp.py``:
  * ``file_name`` / ``experiment_id_or_name`` / ``ids`` are resource keys;
  * ``pod_name`` renders shortened like other long Hope ids;
  * a curated set of *operation* args (method/regex/field/node_id/dry_run)
    renders as up to two compact chips after the resource label.
"""

import unittest

import pytest

from lib.tasks_pkg.tool_display import compose_mcp_display


pytestmark = pytest.mark.unit


def _title(name, args):
    return compose_mcp_display(name, args)[0]


class ModifierChipTest(unittest.TestCase):
    BASE = {
        'cluster': 'shxs_training_cluster',
        'namespace': 'your-user',
        'pod_name': 'psxa9idxrr4mwj8c-worker-0-0',
    }

    def test_file_name_is_resource(self):
        title = _title('mcp__hope__get_log_file', {**self.BASE, 'file_name': 'stdout'})
        self.assertTrue(title.startswith('hope/get_log_file — stdout @ '))
        self.assertIn('shxs_training_cluster/your-user', title)

    def test_method_and_regex_chips(self):
        title = _title('mcp__hope__get_log_file', {
            **self.BASE, 'file_name': 'stdout',
            'method': 'tail', 'regex': 'SMOKE-RJH-001',
        })
        self.assertIn('stdout · tail · /SMOKE-RJH-001/', title)

    def test_incident_batch_titles_all_distinct(self):
        # Exact arg shapes from conversation mtc9t5qiubhy2k round 5 — the
        # four rows that used to render one identical title.
        variants = [
            {'file_name': 'stdout', 'regex': 'SMOKE-RJH-001', 'method': 'tail'},
            {'file_name': 'stdout', 'regex': 'AGENTIX_BRANCH|agentix@', 'method': 'tail'},
            {'file_name': 'stderr', 'regex': 'SMOKE-RJH-001', 'method': 'tail'},
            {'file_name': 'stdout', 'method': 'head'},
        ]
        titles = [_title('mcp__hope__get_log_file', {**self.BASE, **v})
                  for v in variants]
        self.assertEqual(len(titles), len(set(titles)), titles)

    def test_chip_cap_two(self):
        # More than two informative modifiers → only the first two render;
        # the resource stays the primary identity.
        title = _title('mcp__llm__experiment_update_params', {
            'ids': '351488', 'node_id': '2017100', 'field': 'agent',
            'dry_run': True,
        })
        self.assertEqual(title.count(' · '), 2, title)

    def test_dry_run_chip_only_when_true(self):
        with_dry = _title('mcp__llm__experiment_api_serving', {
            'experiment_id': '351488', 'dry_run': True,
        })
        self.assertIn('dry-run', with_dry)
        without = _title('mcp__llm__experiment_api_serving', {
            'experiment_id': '351488', 'dry_run': False,
        })
        self.assertNotIn('dry-run', without)

    def test_chip_equal_to_resource_dropped(self):
        # A chip that merely restates the resource label adds nothing.
        title = _title('mcp__hope__get_log_file', {
            **self.BASE, 'file_name': 'stdout', 'method': 'stdout',
        })
        self.assertNotIn('·', title)

    def test_no_modifier_keys_title_unchanged(self):
        # Regression guard: calls without modifier args keep the old shape.
        self.assertEqual(
            _title('mcp__overleaf__edit_file', {
                'project_id': '6a1e782e9ba0ae3d7727a668',
                'file_path': 'acl_latex.tex',
            }),
            'overleaf/edit_file — acl_latex.tex @ 6a1e7…a668',
        )

    def test_experiment_id_or_name_resource(self):
        self.assertEqual(
            _title('mcp__llm__experiment_get', {'experiment_id_or_name': '351488'}),
            'llm/experiment_get — 351488',
        )

    def test_pod_name_fallback_shortened(self):
        # Without file_name the pod itself is the resource — shortened like
        # other long Hope ids instead of dominating the line.
        title = _title('mcp__hope__list_log_files', dict(self.BASE))
        self.assertIn('psxa9idx…er-0-0', title)
        self.assertNotIn('psxa9idxrr4mwj8c-worker-0-0', title)

    def test_ids_comma_rendering(self):
        title = _title('mcp__llm__experiment_update_params', {'ids': '351488,351489'})
        self.assertIn('351488, 351489', title)

    def test_non_dict_args_safe(self):
        title = _title('mcp__hope__get_log_file', None)
        self.assertEqual(title, 'hope/get_log_file')


if __name__ == '__main__':
    unittest.main()
