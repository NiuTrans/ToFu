"""Insight-engine namespace.

``_run`` owns orchestration; ``_synthesize`` owns model/tool execution;
``_grounding`` owns citation truth; ``_rubric`` owns scoring; and ``_config``
owns feature policy.  Import the concrete owner instead of mutable package
facade state.
"""

__all__: tuple[str, ...] = ()
