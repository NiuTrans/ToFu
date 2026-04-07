#!/usr/bin/env python3
"""Test cross-datacenter FUSE filesystem detection.

Usage: python3 debug/test_cross_dc.py

This test auto-discovers clusters from environment variables.
On machines without FUSE cluster mounts, all tests gracefully skip.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.cross_dc import (
    _build_path_index,
    _parse_cluster_mounts,
    cross_dc_warning,
    get_cluster_for_path,
    get_latency_class,
    get_status,
    get_timeout_multiplier,
    init_cross_dc_detection,
    is_cross_dc,
)


def test_parse():
    """Test cluster mount env var parsing."""
    clusters = _parse_cluster_mounts()
    if not clusters:
        print("⚠ No cluster mount env vars found — skipping parse test")
        return
    print(f"✓ Parsed {len(clusters)} clusters:")
    for name, paths in sorted(clusters.items()):
        print(f"  {name}: {len(paths)} mount(s)")
    assert isinstance(clusters, dict)
    for name, paths in clusters.items():
        assert isinstance(name, str) and name
        assert isinstance(paths, list) and paths
    print()


def test_path_index():
    """Test path→cluster lookup."""
    clusters = _parse_cluster_mounts()
    if not clusters:
        print("⚠ Skipping path index test (no env var)")
        return
    idx = _build_path_index(clusters)
    print(f"✓ Built path index with {len(idx)} entries")
    # Verify longest-prefix ordering
    keys = list(idx.keys())
    for i in range(len(keys) - 1):
        assert len(keys[i]) >= len(keys[i + 1]), \
            f"Path index not sorted: {keys[i]} before {keys[i+1]}"
    print()


def test_synthetic_parse():
    """Test parsing with synthetic data (no env vars needed)."""
    import lib.cross_dc as cdc
    # Temporarily inject a synthetic env var
    test_val = 'clusterA:/mnt/storageA/data,clusterB:/mnt/storageB/data,clusterA:/mnt/storageA/extra'
    old = os.environ.get('CROSS_DC_CLUSTER_MOUNTS', '')
    try:
        os.environ['CROSS_DC_CLUSTER_MOUNTS'] = test_val
        clusters = _parse_cluster_mounts(['CROSS_DC_CLUSTER_MOUNTS'])
        assert 'clusterA' in clusters
        assert 'clusterB' in clusters
        assert len(clusters['clusterA']) == 2
        assert len(clusters['clusterB']) == 1
        print("✓ Synthetic parse test passed")

        idx = _build_path_index(clusters)
        assert len(idx) == 3
        # Longest prefix first
        keys = list(idx.keys())
        for i in range(len(keys) - 1):
            assert len(keys[i]) >= len(keys[i + 1])
        print("✓ Synthetic path index test passed")
    finally:
        if old:
            os.environ['CROSS_DC_CLUSTER_MOUNTS'] = old
        else:
            os.environ.pop('CROSS_DC_CLUSTER_MOUNTS', None)
    print()


def test_detection():
    """Test full detection pipeline (requires real env vars)."""
    from lib.cross_dc import _init
    import lib.cross_dc as cdc

    # Force re-init
    cdc._initialized = False
    _init()

    status = get_status()
    if not status['clusters']:
        print("⚠ No clusters detected — skipping detection test")
        return

    print(f"✓ Detection initialized:")
    print(f"  Local IDC: {status['local_idc'] or '(not set)'}")
    print(f"  Local clusters: {status['local_clusters'] or '(none)'}")
    print(f"  All clusters: {list(status['clusters'].keys())}")

    for name, info in status['clusters'].items():
        lat = info['latency_ms']
        local = "LOCAL" if info['is_local'] else "REMOTE"
        lat_str = f"{lat:.1f}ms" if lat is not None else "N/A"
        print(f"  {name}: {lat_str} ({local})")

    # Test against this project's own path
    project_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cls = get_latency_class(project_path)
    print(f"\n✓ This project ({project_path}):")
    print(f"  Cluster: {get_cluster_for_path(project_path) or '(unknown)'}")
    print(f"  Latency class: {cls}")
    print(f"  Timeout multiplier: {get_timeout_multiplier(project_path)}×")
    print(f"  Is cross-DC: {is_cross_dc(project_path)}")

    warn = cross_dc_warning(project_path)
    if warn:
        print(f"  Warning: {warn}")
    else:
        print(f"  (No warning — local or unknown path)")
    print()


if __name__ == '__main__':
    print("=== Cross-DC Detection Tests ===\n")
    test_parse()
    test_path_index()
    test_synthetic_parse()
    test_detection()
    print("=== All tests passed ===")
