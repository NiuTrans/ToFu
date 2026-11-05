"""Storage comparison workload assembly, independent of concrete database adapters."""
from scripts.tofudb_workloads.catalog import build_workloads
from scripts.tofudb_workloads.inputs import WorkloadInputs, WorkloadPorts

__all__ = ['build_workloads', 'WorkloadInputs', 'WorkloadPorts']
