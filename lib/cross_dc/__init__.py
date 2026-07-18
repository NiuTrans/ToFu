#!/usr/bin/env python3
"""Cross-datacenter FUSE filesystem latency detection and mitigation.

Problem
-------
Distributed FUSE filesystems (e.g. BeeGFS, CephFS, GlusterFS) can mount
paths from multiple storage clusters, some of which may be in remote
datacenters.  When the server is in datacenter A but the user's project is
on a path served by a remote cluster in datacenter B, every file I/O
operation has cross-datacenter round-trip latency — typically 10-50× slower
for metadata (stat, readdir) and even worse for recursive tree walks.

Solution
--------
This module:
  1. Discovers storage clusters via configurable environment variables
  2. Benchmarks each cluster on startup to measure actual I/O latency
  3. Classifies clusters as local / slow / very_slow based on thresholds
  4. Provides ``get_timeout_multiplier(path)`` for adaptive timeout adjustment
  5. Provides ``cross_dc_warning(path)`` for tool-level user warnings

Configuration
-------------
All behavior is driven by environment variables and optional config overrides.
No paths, cluster names, or datacenter identifiers are hardcoded.

**Environment variables** (auto-detected):
  - ``CROSS_DC_CLUSTER_MOUNTS`` — Primary. Format: ``cluster1:/path/a,cluster2:/path/b,...``
  - ``LIBBGFS_CLUSTERMOUNTPATHS`` — Fallback (BeeGFS-specific).  Same format.
  - ``CROSS_DC_LOCAL_IDC`` — Override for local datacenter identifier.
  - ``HULK_IDC`` — Fallback for local datacenter identifier.

**Config file** (optional): ``data/config/cross_dc.json``
  .. code-block:: json

     {
       "cluster_mounts_env": "MY_CUSTOM_ENV_VAR",
       "local_idc_env": "MY_IDC_VAR",
       "slow_threshold_ms": 10,
       "very_slow_threshold_ms": 30,
       "slow_timeout_multiplier": 3,
       "very_slow_timeout_multiplier": 5,
       "enabled": true
     }

**On machines without these env vars, the module is a silent no-op.**

Usage
-----
Called from ``set_project()`` and tool dispatch::

    from lib.cross_dc import is_cross_dc, get_timeout_multiplier, cross_dc_warning

    if is_cross_dc(project_path):
        timeout *= get_timeout_multiplier(project_path)

Package layout
--------------
This module was decomposed from a monolithic ``lib/cross_dc.py`` into a
facade-preserving package:
  - ``_probe.py`` — pure latency probing (``_probe_latency``), no shared state
  - ``_state.py`` — ALL process-wide detection singletons + every function
    that ``global``-rebinds or reads them (config, init, benchmark, path
    index, and the public reader APIs)

This ``__init__`` re-exports every public symbol so all existing
``from lib.cross_dc import X`` imports keep working byte-identically.
"""

from lib.log import get_logger

# ── Pure probe helpers (no shared state) ──
from lib.cross_dc._probe import (
    _PROBE_TIMEOUT_S,
    _probe_latency,
)

# ── Core state + all globals-touching functions & public API ──
from lib.cross_dc._state import (
    # Config constants (some rebound by _apply_config)
    _BENCHMARK_TTL_S,
    _CONFIG_FILE,
    _DEFAULT_CLUSTER_MOUNTS_ENVS,
    _DEFAULT_LOCAL_IDC_ENVS,
    _SLOW_THRESHOLD_S,
    _SLOW_TIMEOUT_MULTIPLIER,
    _VERY_SLOW_THRESHOLD_S,
    _VERY_SLOW_TIMEOUT_MULTIPLIER,
    # Shared state singletons
    _benchmark_event,
    _lock,
    # Private helpers (imported/patched by debug/test_cross_dc.py etc.)
    _apply_config,
    _benchmark_clusters,
    _build_path_index,
    _ensure_initialized,
    _find_env_value,
    _init,
    _load_config,
    _parse_cluster_mounts,
    # Public API
    cross_dc_warning,
    get_cluster_for_path,
    get_latency_class,
    get_latency_s,
    get_status,
    get_timeout_multiplier,
    init_cross_dc_detection,
    is_cross_dc,
)

logger = get_logger(__name__)

__all__ = [
    # Public API
    'init_cross_dc_detection',
    'get_cluster_for_path',
    'is_cross_dc',
    'get_latency_s',
    'get_latency_class',
    'get_timeout_multiplier',
    'cross_dc_warning',
    'get_status',
    # Private helpers (used by debug/test_cross_dc.py)
    '_init',
    '_apply_config',
    '_load_config',
    '_ensure_initialized',
    '_build_path_index',
    '_benchmark_clusters',
    '_parse_cluster_mounts',
    '_find_env_value',
    '_probe_latency',
    # Constants
    '_PROBE_TIMEOUT_S',
    '_SLOW_THRESHOLD_S',
    '_VERY_SLOW_THRESHOLD_S',
    '_SLOW_TIMEOUT_MULTIPLIER',
    '_VERY_SLOW_TIMEOUT_MULTIPLIER',
    '_BENCHMARK_TTL_S',
    '_CONFIG_FILE',
    '_DEFAULT_CLUSTER_MOUNTS_ENVS',
    '_DEFAULT_LOCAL_IDC_ENVS',
    # Shared state
    '_lock',
    '_benchmark_event',
]
