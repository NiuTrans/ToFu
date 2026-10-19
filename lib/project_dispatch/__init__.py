"""Explicit human-authorized project consultation dispatch (no tool authority)."""
from .repository import ProjectDispatchError, ProjectDispatchRepository

__all__ = ['ProjectDispatchRepository', 'ProjectDispatchError']
