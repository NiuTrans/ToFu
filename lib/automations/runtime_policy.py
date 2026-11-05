"""Automation actions borrow the application's single worker runtime policy."""
from lib.durable_worker.runtime_policy import runtime_policy

__all__ = ['runtime_policy']
