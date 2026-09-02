"""Versioned experiment capabilities, assignment, analysis, and discovery.

Entry points: :func:`registry`, :func:`resolve_experiment_spec`, and
:func:`apply_experiment`.  Storage and HTTP adapters depend on these contracts;
the package never imports routes or a concrete database backend.
"""

from .contracts import (
    CONTRACT_VERSION,
    ExperimentContractError,
    resolve_experiment_spec,
    validate_resolved_spec,
)
from .registry import (
    AnalyzerProvider,
    ExperimentPlugin,
    MetricProvider,
    StrategyProvider,
    registry,
)
from .service import (
    analyze_experiment,
    apply_experiment,
    assign_experiment,
    compile_experiment_application,
    compile_metric_extractor,
    extract_metric_values,
)

__all__ = [
    "AnalyzerProvider",
    "CONTRACT_VERSION",
    "ExperimentContractError",
    "ExperimentPlugin",
    "MetricProvider",
    "StrategyProvider",
    "analyze_experiment",
    "apply_experiment",
    "assign_experiment",
    "compile_experiment_application",
    "compile_metric_extractor",
    "extract_metric_values",
    "registry",
    "resolve_experiment_spec",
    "validate_resolved_spec",
]
