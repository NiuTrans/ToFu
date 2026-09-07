"""Tool-inventory drift guard + the write/approval ratchet.

Two jobs, deliberately in one file because they share the generator:

1. **Drift guard** — ``docs/TOOL_INVENTORY.md`` must match what
   ``scripts/gen_tool_inventory.py`` derives from the live registry. The
   inventory exists so a reviewer (human or model) can see all ~90 tools ×
   their facets at once; an inventory that silently goes stale is worse than
   none, because it invites decisions based on a fiction.

2. **Approval-enricher ratchet** — every tool in the write partition must have
   an entry in ``_APPROVAL_META_ENRICHERS``, with a grandfathered exemption
   list that may only SHRINK.

   Why this is a real defect class and not a style preference: the base
   approval metadata is built from ``fn_args['path']`` and
   ``fn_args['description']`` only (``_approval.py::_handle_approval``). A
   tool whose risk lives in some OTHER argument — ``command`` for
   ``desktop_run_command``, ``selector`` for ``browser_click``,
   ``memory_id`` for ``delete_memory`` — therefore renders an approval dialog
   with those fields EMPTY. The user is asked to approve
   ``desktop_run_command`` without being shown ``rm -rf ~/Documents``.
   ``_approval.py`` states the rule itself: "Without an enricher the prompt
   renders a bare tool name and the user approves blind — false confidence,
   worse than not prompting at all." It was applied to the browser_execute_js /
   schedule_create batch and missed for the rest of the same batch.

The ratchet follows the charter's precedent for
``test_error_transparency_guard``: known offenders are grandfathered with a
meta-assertion that the exemption list contains no dead entries, so cleaning
up the list can't silently re-open the hole.
"""

import subprocess
import sys

import pytest

pytestmark = pytest.mark.unit

GEN = 'scripts/gen_tool_inventory.py'


def _repo_root():
    import os
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _model_visible_write_tools():
    """Write-partition tools the MODEL can actually call.

    Intersected with ``provides`` on purpose: ``desktop_move_file`` sits in the
    desktop spec's ``write_tools`` but is deliberately absent from ``provides``
    (never exposed to the LLM — see lib/desktop_tools.py). It cannot reach the
    approval dialog, so requiring an enricher for it would be noise.
    """
    import lib.tasks_pkg.handlers  # noqa: F401 — registration side-effect
    from lib.tasks_pkg.tool_dispatch._flags import _WRITE_TOOLS
    from lib.tools.registry import all_specs

    provides = set()
    for s in all_specs():
        if s.source != 'plugin':
            provides |= set(s.provides)
    return {t for t in _WRITE_TOOLS if t in provides}


#: Write tools that reach the approval dialog WITHOUT rendering the
#: risk-bearing argument. **NOW EMPTY** — all 33 model-visible write tools were
#: fixed (18 new enrichers + 8 existing ones that wrote keys the renderer did
#: not understand). It stays as an explicit empty set so a future regression has
#: to justify re-opening the hole rather than silently appending a name.
#:
#: NOTE: "has an enricher" is NOT the criterion — 8 enrichers used to exist and
#: still rendered a blank dialog. The real contract (dialog shows the risk) is
#: proven end-to-end in tests/test_approval_dialog_renders_risk.py, which runs
#: the SHIPPED frontend renderer. This list may only stay empty or shrink.
GRANDFATHERED_NO_ENRICHER: set[str] = set()


class TestInventoryNotStale:
    def test_generated_inventory_matches_live_registry(self):
        """`--check` must pass: the committed inventory reflects the code."""
        proc = subprocess.run(
            [sys.executable, GEN, '--check'],
            cwd=_repo_root(), capture_output=True, text=True, timeout=600,
        )
        assert proc.returncode == 0, (
            f'tool inventory is stale — run `python3 {GEN}` and commit '
            f'docs/TOOL_INVENTORY.md.\nstderr: {proc.stderr[-2000:]}'
        )
        assert 'pthread_setaffinity_np failed' not in proc.stderr, (
            'read-only inventory generation constructed uncapped ONNX '
            'sessions; install the ONNX thread guard before handler imports'
        )

    def test_generator_reports_every_declared_builtin(self):
        """Every built-in in the registry appears as an inventory row.

        Asserts the RESULT (coverage) rather than the file's text, so the
        report layout can be rewritten without falsely failing.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import collect

        from lib.tools.registry import all_specs
        inv = collect()
        rows = {r['tool'] for r in inv['builtin']}
        declared = set()
        for s in all_specs():
            if s.source != 'plugin':
                declared |= set(s.provides)
        assert declared - rows == set(), (
            f'declared built-ins missing from the inventory: '
            f'{sorted(declared - rows)}'
        )

    def test_plugin_tools_are_not_in_the_pinned_table(self):
        """Plugin rows stay in the diagnostic section.

        A deployment-specific plugin inside the `--check`ed table would make CI
        red on any host with a different plugin set.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import collect
        inv = collect()
        builtin = {r['tool'] for r in inv['builtin']}
        for r in inv['plugin']:
            assert r['tool'] not in builtin


class TestConfusableDetectorHasControls:
    """Controls for the ONE judgement column that survived calibration.

    Two sibling columns were cut during this work, and the reason both died is
    the reason this class exists:

    * ``returns`` (MARKED/CAPPED/UNBOUNDED) — five designs, each falsified
      against source-verified tools; the last traded 3 false positives for 4
      false negatives.
    * ``params_ok`` — reported ``0`` defects, but fed its OWN motivating
      precedent as a synthetic schema it returned no defect. Zero because the
      rule fired on nothing, not because the surface was clean.

    A detector reporting a low number is indistinguishable from a detector that
    does not work, so ``describes_ok`` is pinned from both directions: a
    known-bad input it MUST flag, and a known-good input it must NOT.
    """

    def test_positive_control_near_duplicate_pair_is_flagged(self):
        """The rule must flag a pair engineered to be confusable.

        This is exactly the check ``params_ok`` failed. Without it, a
        ``confusable: 0`` line would be untrustworthy.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import _confusable_pairs

        schemas = {
            'read_thing': {'description':
                           'Read the contents of a thing from the project store.'},
            'read_object': {'description':
                            'Read the contents of an object from the project store.'},
        }
        rows = [{'tool': 'read_thing', 'category': 'files'},
                {'tool': 'read_object', 'category': 'files'}]
        pairs = _confusable_pairs(schemas, rows)
        assert pairs, (
            'the confusable detector failed its POSITIVE CONTROL: two '
            'descriptions differing only by a synonym were not flagged, so a '
            'low reported count means nothing'
        )

    def test_negative_control_unrelated_siblings_are_not_flagged(self):
        """Same category, genuinely different jobs -> not confusable.

        Guards the opposite failure: a threshold loose enough to flag everything
        is equally useless (an earlier column read 89/89 for one value).
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import _confusable_pairs

        schemas = {
            'read_files': {'description':
                           'Read the contents of one or more files.'},
            'run_command': {'description':
                            'Execute a shell command and return its output.'},
        }
        rows = [{'tool': 'read_files', 'category': 'files'},
                {'tool': 'run_command', 'category': 'files'}]
        assert _confusable_pairs(schemas, rows) == [], (
            'unrelated same-category tools were reported confusable — the '
            'threshold is too loose to discriminate'
        )

    def test_cross_category_pairs_are_not_compared(self):
        """Identical wording in unrelated categories is not the model's problem.

        Pins the scoping decision: the choice a model faces is between
        neighbours, and comparing across the whole surface manufactures noise.
        """
        sys.path.insert(0, _repo_root())
        from scripts.gen_tool_inventory import _confusable_pairs

        desc = 'Read the contents of a thing from the store.'
        schemas = {'a_tool': {'description': desc},
                   'b_tool': {'description': desc}}
        rows = [{'tool': 'a_tool', 'category': 'files'},
                {'tool': 'b_tool', 'category': 'browser'}]
        assert _confusable_pairs(schemas, rows) == []


class TestApprovalEnricherRatchet:
    def test_every_write_tool_has_an_approval_enricher(self):
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS

        missing = _model_visible_write_tools() - set(_APPROVAL_META_ENRICHERS)
        new = missing - GRANDFATHERED_NO_ENRICHER
        assert not new, (
            f'{len(new)} write tool(s) reach the approval dialog with no '
            f'enricher: {sorted(new)}. The dialog would render a bare tool '
            f'name (base meta is only path+description), so the user approves '
            f'without seeing what runs. Add an enricher in '
            f'lib/tasks_pkg/tool_dispatch/_approval.py that surfaces the '
            f'risk-bearing argument.'
        )

    def test_grandfather_list_has_no_dead_entries(self):
        """A fixed tool must leave the exemption list.

        Without this, the list keeps "protecting" tools that no longer need it
        and the ratchet stops being able to tighten — the exact rot pattern
        that produced the dead-allowlist family.
        """
        from lib.tasks_pkg.tool_dispatch._approval import _APPROVAL_META_ENRICHERS

        write_tools = _model_visible_write_tools()
        fixed = {t for t in GRANDFATHERED_NO_ENRICHER
                 if t in _APPROVAL_META_ENRICHERS}
        assert not fixed, (
            f'these tools now HAVE an enricher — remove them from '
            f'GRANDFATHERED_NO_ENRICHER so the ratchet tightens: {sorted(fixed)}'
        )
        gone = GRANDFATHERED_NO_ENRICHER - write_tools
        assert not gone, (
            f'these grandfathered names are no longer model-visible write '
            f'tools — drop them: {sorted(gone)}'
        )

    def test_base_meta_alone_cannot_surface_a_risky_argument(self):
        """Pins WHY every write tool needs an enricher, in behavioural terms.

        The base metadata is built from ``path`` + ``description`` only, so a
        tool whose risk lives in another argument (``command``, ``selector``,
        ``memory_id``) is invisible without an enricher. Asserted against the
        base shape itself rather than against a still-unfixed tool, so the
        premise survives every tool being fixed.
        """
        base = {'approvalId': 'x', 'toolName': 'desktop_run_command',
                'path': '', 'description': ''}
        rendered = ' '.join(str(v) for v in base.values())
        assert 'rm -rf' not in rendered, (
            'base approval meta unexpectedly surfaces a command — if the base '
            'shape became self-describing, re-evaluate this ratchet'
        )
