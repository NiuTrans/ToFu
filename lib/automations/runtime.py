"""Compatibility imports for the application's shared durable worker owner.

Automation kind assembly lives in worker_registration. This module creates no
service, lock, extension entry or independent lifecycle owner of its own.
"""
from lib.durable_worker.runtime import create_service, ensure_workers_started, stop_workers

__all__ = ['create_service', 'ensure_workers_started', 'stop_workers']
