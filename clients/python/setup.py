"""Compatibility shim for installers that still look for ``setup.py``.

All package metadata lives in ``pyproject.toml`` so releases have one version
and dependency source of truth.

Usage during development::

    cd clients/python && pip install -e .

Or vendored into a downstream project::

    pip install /path/to/tofu/clients/python
"""

from setuptools import setup


setup()
