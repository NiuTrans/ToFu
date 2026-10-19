"""Real browser five-case reader, interaction, CPU and memory mechanics."""
import os
from pathlib import Path
import pytest
from tests.support.frontend_visual import frontend_visual_origin as frontend_visual_origin  # noqa: F401
from benchmarks.markdown_worker.domain_reader import measure

pytestmark=pytest.mark.visual


def test_markdown_domain_smoke(browser,frontend_visual_origin,tmp_path):
    reports=measure(browser,frontend_visual_origin,tmp_path,Path(os.environ.get('TOFU_MARKDOWN_SMOKE_OUTPUT',str(tmp_path/'domain-smoke'))),samples=2,pairs=1)
    assert all(len(r['workloads'])==5 for r in reports.values())


def test_markdown_domain_full(browser,frontend_visual_origin,tmp_path):
    output=os.environ.get('TOFU_MARKDOWN_DOMAIN_OUTPUT')
    if not output:pytest.skip('set TOFU_MARKDOWN_DOMAIN_OUTPUT for full independent cohorts')
    reports = measure(browser,frontend_visual_origin,tmp_path,output)
    assert reports
    assert all(len(report['workloads']) == 5 for report in reports.values())
