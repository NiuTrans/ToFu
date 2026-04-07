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
"""

import json
import os
import threading
import time

from lib.log import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Configuration — all overridable via data/config/cross_dc.json
# ═══════════════════════════════════════════════════════════════════════

# Default env var names to probe for cluster mount info
_DEFAULT_CLUSTER_MOUNTS_ENVS = ['CROSS_DC_CLUSTER_MOUNTS', 'LIBBGFS_CLUSTERMOUNTPATHS']
_DEFAULT_LOCAL_IDC_ENVS = ['CROSS_DC_LOCAL_IDC', 'HULK_IDC']

# If stat(nonexistent) takes longer than this, the cluster is "slow" (likely cross-DC)
_SLOW_THRESHOLD_S = 0.010    # 10ms

# If stat(nonexistent) takes longer than this, it's "very slow" (definitely cross-DC)
_VERY_SLOW_THRESHOLD_S = 0.030  # 30ms

# Timeout multipliers for cross-DC paths
_SLOW_TIMEOUT_MULTIPLIER = 3       # 3× timeout for moderately slow clusters
_VERY_SLOW_TIMEOUT_MULTIPLIER = 5  # 5× timeout for very slow clusters

# Benchmark probe timeout per cluster
_PROBE_TIMEOUT_S = 10.0

# Cache duration — re-benchmark if older than this
_BENCHMARK_TTL_S = 3600  # 1 hour

_CONFIG_FILE = os.path.join('data', 'config', 'cross_dc.json')


def _load_config():
    """Load optional config overrides from data/config/cross_dc.json.

    Returns empty dict if file doesn't exist (the common case).
    """
    try:
        if os.path.isfile(_CONFIG_FILE):
            with open(_CONFIG_FILE, 'r') as f:
                cfg = json.load(f)
            logger.info('[CrossDC] Loaded config from %s', _CONFIG_FILE)
            return cfg
    except (json.JSONDecodeError, OSError) as e:
        logger.warning('[CrossDC] Failed to read config %s: %s', _CONFIG_FILE, e)
    return {}


def _apply_config(cfg):
    """Apply config overrides to module-level settings."""
    global _SLOW_THRESHOLD_S, _VERY_SLOW_THRESHOLD_S
    global _SLOW_TIMEOUT_MULTIPLIER, _VERY_SLOW_TIMEOUT_MULTIPLIER

    if 'slow_threshold_ms' in cfg:
        _SLOW_THRESHOLD_S = cfg['slow_threshold_ms'] / 1000.0
    if 'very_slow_threshold_ms' in cfg:
        _VERY_SLOW_THRESHOLD_S = cfg['very_slow_threshold_ms'] / 1000.0
    if 'slow_timeout_multiplier' in cfg:
        _SLOW_TIMEOUT_MULTIPLIER = cfg['slow_timeout_multiplier']
    if 'very_slow_timeout_multiplier' in cfg:
        _VERY_SLOW_TIMEOUT_MULTIPLIER = cfg['very_slow_timeout_multiplier']


# ═══════════════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════════════

_lock = threading.Lock()

# cluster_name → { 'paths': ['/mnt/...', ...], 'latency_s': float|None }
_clusters: dict = {}

# path_prefix → cluster_name (longest-prefix match table)
_path_to_cluster: dict = {}

# The local IDC identifier (from env var)
_local_idc: str = ''

# The cluster name(s) that are local (determined by benchmark)
_local_clusters: set = set()

# When the last benchmark was run
_last_benchmark: float = 0.0

# Whether initialization has been done
_initialized: bool = False


# ═══════════════════════════════════════════════════════════════════════
#  Initialization — discover cluster mounts from environment
# ═══════════════════════════════════════════════════════════════════════

def _find_env_value(env_names: list[str]) -> str:
    """Try each env var name in order, return first non-empty value."""
    for name in env_names:
        val = os.environ.get(name, '').strip()
        if val:
            return val
    return ''


def _parse_cluster_mounts(cluster_mounts_envs: list[str] | None = None):
    """Parse cluster mount paths from environment variables.

    Expected format: ``cluster1:/path/a,cluster1:/path/b,cluster2:/path/c,...``

    The env var name is configurable — tries each name in order until one
    is found.  This makes the module work with any FUSE filesystem that
    exposes a similar env var, not just BeeGFS.

    Returns:
        dict: cluster_name → list of mount paths
    """
    envs = cluster_mounts_envs or _DEFAULT_CLUSTER_MOUNTS_ENVS
    raw = _find_env_value(envs)
    if not raw:
        return {}

    clusters = {}
    for entry in raw.split(','):
        entry = entry.strip()
        if ':' not in entry:
            continue
        # Split on first ':' — cluster name does not contain ':'
        # but paths always start with '/'
        colon_idx = entry.index(':')
        cluster_name = entry[:colon_idx].strip()
        mount_path = entry[colon_idx + 1:].strip()

        if not cluster_name or not mount_path:
            continue

        if cluster_name not in clusters:
            clusters[cluster_name] = []
        if mount_path not in clusters[cluster_name]:
            clusters[cluster_name].append(mount_path)

    return clusters


def _build_path_index(clusters):
    """Build a path-prefix → cluster lookup table.

    Sorted by path length descending for longest-prefix matching.
    """
    index = {}
    for cluster_name, paths in clusters.items():
        for path in paths:
            # Normalize: ensure trailing slash for prefix matching
            key = path.rstrip('/') + '/'
            index[key] = cluster_name
    return dict(sorted(index.items(), key=lambda x: -len(x[0])))


def _probe_latency(path, timeout=_PROBE_TIMEOUT_S):
    """Measure real I/O latency to a storage cluster.

    Uses stat() on a non-existent path to avoid FUSE metadata cache hits.
    Cached stat() on existing paths returns ~0ms even for cross-DC clusters,
    so we must probe paths that force a round-trip to the metadata server.

    Returns latency in seconds, or None if the probe timed out.
    """
    import random
    result = [None]
    event = threading.Event()

    def _do_probe():
        t0 = time.monotonic()
        try:
            # Probe a non-existent path to bypass FUSE metadata cache
            probe_name = f'_latency_probe_{random.randint(100000, 999999)}'
            probe_path = os.path.join(path, probe_name)
            try:
                os.stat(probe_path)
            except FileNotFoundError:
                pass  # Expected — we're measuring the round-trip time
            result[0] = time.monotonic() - t0
        except OSError:
            # Mount point itself is inaccessible
            result[0] = time.monotonic() - t0
        finally:
            event.set()

    t = threading.Thread(target=_do_probe, daemon=True, name='cross-dc-probe')
    t.start()
    completed = event.wait(timeout=timeout)

    if not completed:
        logger.warning('[CrossDC] Probe timed out for %s (>%.0fs)', path, timeout)
        return None

    return result[0]


def _benchmark_clusters(clusters):
    """Benchmark all known clusters and classify as local/remote.

    Returns:
        dict: cluster_name → latency_s (float)
        set: local cluster names
    """
    latencies = {}
    local = set()

    for cluster_name, paths in clusters.items():
        # Pick the first path that exists for benchmarking
        test_path = None
        for p in paths:
            if os.path.exists(p):
                test_path = p
                break

        if not test_path:
            logger.debug('[CrossDC] No accessible path for cluster %s, skipping', cluster_name)
            continue

        # Run 3 probes and take the median
        probes = []
        for _ in range(3):
            lat = _probe_latency(test_path, timeout=5.0)
            if lat is not None:
                probes.append(lat)

        if not probes:
            logger.warning('[CrossDC] All probes failed for cluster %s', cluster_name)
            latencies[cluster_name] = None
            continue

        median_lat = sorted(probes)[len(probes) // 2]
        latencies[cluster_name] = median_lat

        if median_lat < _SLOW_THRESHOLD_S:
            local.add(cluster_name)
            logger.info('[CrossDC] Cluster %-20s → %.1fms (LOCAL)', cluster_name, median_lat * 1000)
        elif median_lat < _VERY_SLOW_THRESHOLD_S:
            logger.info('[CrossDC] Cluster %-20s → %.1fms (SLOW — likely cross-DC)',
                        cluster_name, median_lat * 1000)
        else:
            logger.info('[CrossDC] Cluster %-20s → %.1fms (VERY SLOW — cross-DC)',
                        cluster_name, median_lat * 1000)

    return latencies, local


def _init():
    """Initialize cross-DC detection (idempotent, thread-safe)."""
    global _clusters, _path_to_cluster, _local_idc, _local_clusters
    global _last_benchmark, _initialized

    with _lock:
        if _initialized and (time.monotonic() - _last_benchmark) < _BENCHMARK_TTL_S:
            return

        # Load optional config overrides
        cfg = _load_config()
        if cfg.get('enabled') is False:
            logger.info('[CrossDC] Disabled via config')
            _initialized = True
            return

        _apply_config(cfg)

        # Determine which env vars to read
        cluster_envs = _DEFAULT_CLUSTER_MOUNTS_ENVS
        idc_envs = _DEFAULT_LOCAL_IDC_ENVS

        if cfg.get('cluster_mounts_env'):
            cluster_envs = [cfg['cluster_mounts_env']] + cluster_envs
        if cfg.get('local_idc_env'):
            idc_envs = [cfg['local_idc_env']] + idc_envs

        _local_idc = _find_env_value(idc_envs)

        raw_clusters = _parse_cluster_mounts(cluster_envs)
        if not raw_clusters:
            logger.debug('[CrossDC] No cluster mount env vars found — cross-DC detection disabled')
            _initialized = True
            return

        logger.info('[CrossDC] Found %d storage clusters, local IDC=%s. Benchmarking...',
                     len(raw_clusters), _local_idc or '(unknown)')

        _path_to_cluster = _build_path_index(raw_clusters)

        latencies, local = _benchmark_clusters(raw_clusters)

        # Update state
        for cluster_name, paths in raw_clusters.items():
            _clusters[cluster_name] = {
                'paths': paths,
                'latency_s': latencies.get(cluster_name),
            }

        _local_clusters = local
        _last_benchmark = time.monotonic()
        _initialized = True

        logger.info('[CrossDC] Initialization complete: %d clusters, %d local (%s), %d remote',
                     len(_clusters), len(_local_clusters),
                     ', '.join(sorted(_local_clusters)) or 'none',
                     len(_clusters) - len(_local_clusters))


def _ensure_initialized():
    """Ensure init has run. Called lazily on first use."""
    if not _initialized:
        _init()


# ═══════════════════════════════════════════════════════════════════════
#  Public API
# ═══════════════════════════════════════════════════════════════════════

def get_cluster_for_path(path: str) -> str | None:
    """Return the storage cluster name serving the given path, or None.

    Uses longest-prefix matching against known mount paths.
    """
    _ensure_initialized()

    if not path or not _path_to_cluster:
        return None

    abs_path = os.path.abspath(path).rstrip('/') + '/'

    for prefix, cluster_name in _path_to_cluster.items():
        if abs_path.startswith(prefix):
            return cluster_name

    return None


def is_cross_dc(path: str) -> bool:
    """Check if a path is served by a remote (cross-datacenter) cluster.

    Returns:
        True if the path is on a known remote cluster.
        False if local, unknown, or cross-DC detection is not available.
    """
    _ensure_initialized()

    cluster = get_cluster_for_path(path)
    if cluster is None:
        return False

    return cluster not in _local_clusters


def get_latency_s(path: str) -> float | None:
    """Get the measured latency (seconds) for the cluster serving this path.

    Returns None if unknown.
    """
    _ensure_initialized()

    cluster = get_cluster_for_path(path)
    if cluster is None or cluster not in _clusters:
        return None

    return _clusters[cluster].get('latency_s')


def get_latency_class(path: str) -> str:
    """Classify a path's latency as 'local', 'slow', or 'very_slow'.

    Returns:
        'local' — same datacenter, fast I/O
        'slow' — cross-DC, moderate latency
        'very_slow' — cross-DC, high latency
        'unknown' — not a known FUSE-mounted path
    """
    _ensure_initialized()

    lat = get_latency_s(path)
    if lat is None:
        return 'unknown'

    if lat < _SLOW_THRESHOLD_S:
        return 'local'
    elif lat < _VERY_SLOW_THRESHOLD_S:
        return 'slow'
    else:
        return 'very_slow'


def get_timeout_multiplier(path: str) -> float:
    """Get timeout multiplier for operations on this path.

    Returns:
        1.0 for local paths (no adjustment needed)
        3.0 for slow (moderately cross-DC) paths  (configurable)
        5.0 for very slow (highly cross-DC) paths  (configurable)
    """
    cls = get_latency_class(path)
    if cls == 'slow':
        return _SLOW_TIMEOUT_MULTIPLIER
    elif cls == 'very_slow':
        return _VERY_SLOW_TIMEOUT_MULTIPLIER
    return 1.0


def cross_dc_warning(path: str) -> str:
    """Generate a user-facing warning string if path is cross-DC.

    Returns an empty string if the path is local or unknown.
    """
    _ensure_initialized()

    cluster = get_cluster_for_path(path)
    if cluster is None or cluster in _local_clusters:
        return ''

    lat = get_latency_s(path)
    lat_str = f'{lat * 1000:.0f}ms' if lat is not None else '?ms'

    return (
        f'⚠️ Cross-datacenter path detected: cluster={cluster}, '
        f'latency={lat_str}, local_idc={_local_idc}. '
        f'File operations will be slower than usual.'
    )


def get_status() -> dict:
    """Return current cross-DC detection status for diagnostics."""
    _ensure_initialized()

    return {
        'local_idc': _local_idc,
        'clusters': {
            name: {
                'paths': info['paths'],
                'latency_ms': round(info['latency_s'] * 1000, 1) if info['latency_s'] else None,
                'is_local': name in _local_clusters,
            }
            for name, info in _clusters.items()
        },
        'local_clusters': sorted(_local_clusters),
        'initialized': _initialized,
        'last_benchmark_ago_s': round(time.monotonic() - _last_benchmark, 0) if _last_benchmark else None,
    }


# ═══════════════════════════════════════════════════════════════════════
#  Startup hook — called from server.py
# ═══════════════════════════════════════════════════════════════════════

def init_cross_dc_detection():
    """Initialize cross-DC detection in a background thread.

    Safe to call from server startup — benchmarking runs in background
    so it doesn't block the web server from starting.

    On machines without the relevant env vars, this is a fast no-op.
    """
    def _bg_init():
        try:
            _init()
        except Exception as e:
            logger.error('[CrossDC] Background init failed: %s', e, exc_info=True)

    t = threading.Thread(target=_bg_init, daemon=True, name='cross-dc-init')
    t.start()
    logger.info('[CrossDC] Background initialization started')
