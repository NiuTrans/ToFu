"""Executable contracts for request-loaded arXiv parsing and search."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_isolated(source: str) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != 'LD_PRELOAD'}
    return subprocess.run(
        [sys.executable, '-c', source], cwd=_REPO, env=env, timeout=240,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.mark.unit
def test_fetch_route_import_keeps_arxiv_parser_dormant():
    proc = _run_isolated(
        'import sys; import routes.paper_pkg._arxiv as route; '
        'print("ARXIV-FETCH-ROUTE", callable(route._extract_arxiv_id), '
        'callable(route.fetch_arxiv_title), '
        '"lib.paper.arxiv" in sys.modules, '
        '"xml.etree.ElementTree" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'ARXIV-FETCH-ROUTE True True False False' in proc.stdout


@pytest.mark.unit
def test_search_route_import_keeps_arxiv_parser_dormant():
    proc = _run_isolated(
        'import sys; import routes.paper_pkg._recommend as route; '
        'print("ARXIV-SEARCH-ROUTE", callable(route.search_arxiv_explained), '
        'route.ArxivQuerySyntaxError.__module__, '
        '"lib.paper.arxiv" in sys.modules, '
        '"xml.etree.ElementTree" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (
        'ARXIV-SEARCH-ROUTE True lib.paper.arxiv_errors False False'
        in proc.stdout
    )


@pytest.mark.unit
def test_server_boot_keeps_arxiv_parser_dormant():
    proc = _run_isolated(
        'import sys; import server; '
        'print("SERVER-ARXIV", '
        '"routes.paper_pkg._arxiv" in sys.modules, '
        '"routes.paper_pkg._recommend" in sys.modules, '
        '"lib.paper.arxiv_errors" in sys.modules, '
        '"lib.paper.arxiv" in sys.modules, '
        '"xml.etree.ElementTree" in sys.modules)'
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert 'SERVER-ARXIV True True True False False' in proc.stdout


@pytest.mark.unit
def test_public_arxiv_error_keeps_one_identity():
    from lib.paper.arxiv_errors import ArxivQuerySyntaxError as light_error
    from lib.paper.arxiv import ArxivQuerySyntaxError as public_error

    assert public_error is light_error
