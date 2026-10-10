"""Dependency-free worker ownership for Tofu application and CLI helpers."""
from .executor import DemandExecutor, DemandThreadPoolExecutor

__all__ = ['DemandExecutor', 'DemandThreadPoolExecutor']
