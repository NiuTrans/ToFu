"""setup.py — Tofu Python SDK packaging.

Usage during development::

    cd clients/python && pip install -e .

Or vendored into a downstream project::

    pip install /path/to/tofu/clients/python
"""

from setuptools import setup, find_packages

setup(
    name='tofu-sdk',
    version='1.0.0',
    description='Python client for the Tofu headless API',
    author='Tofu contributors',
    license='MIT',
    packages=find_packages(),
    python_requires='>=3.9',
    install_requires=['requests>=2.28.0'],
    extras_require={
        'cli': ['click>=8.0.0'],
    },
    entry_points={
        'console_scripts': [
            'tofu = tofu_sdk._cli:main [cli]',
        ],
    },
)
