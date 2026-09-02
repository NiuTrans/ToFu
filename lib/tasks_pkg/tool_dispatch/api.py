"""Stable tool-dispatch service contract.

This module is the only cross-domain entry point for parsing model tool calls
and executing them through approval, scheduling, receipt, and result handling.
"""

from lib.tasks_pkg.tool_dispatch._parse import parse_tool_calls
from lib.tasks_pkg.tool_dispatch._pipeline import execute_tool_pipeline

__all__ = ['execute_tool_pipeline', 'parse_tool_calls']
