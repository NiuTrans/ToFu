"""Hermetic agent benchmark orchestration.

The package intentionally contains orchestration only.  Agent execution is
delegated to Harbor for SWE-bench Verified and Terminal-Bench 2.1. Patch grading
is delegated to the upstream SWE-bench harness so the project does not grow a
second, subtly different verifier.
"""

from .constants import FRAMEWORK_VERSION

__all__ = ["FRAMEWORK_VERSION"]
