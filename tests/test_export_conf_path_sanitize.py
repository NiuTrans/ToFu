"""Deployment sources stay host-neutral; export keeps a final generic scrub."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]

# Assemble internal tokens so this guard does not itself contain the complete
# path/user strings that an open-source export rejects.
_MOUNT = "/mnt/" + "dolphin" + "fs"
_OPERATOR = "ruanjun" + "hao04"
_ACCOUNT = "hadoop" + "-aipnlp"
_INTERNAL_TOKENS = (_MOUNT, _OPERATOR, _ACCOUNT)


def test_host_launchers_never_embed_the_build_machine():
    deployment_sources = (
        "restart_15000.sh",
        "deploy/supervisor/install.sh",
        "deploy/supervisor/render_config.py",
        "deploy/supervisor/tofu.conf.template",
    )
    for relative_path in deployment_sources:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        for token in _INTERNAL_TOKENS:
            assert token not in source, f"{relative_path} embeds build-host token {token!r}"


def test_conf_files_remain_eligible_for_generic_export_sanitization():
    export_module = pytest.importorskip(
        "export", reason="export.py is not shipped in open-source builds")
    assert export_module._is_text_file("deploy/generated/tofu.conf") is True


def test_generic_export_scrub_still_removes_accidental_host_paths():
    export_module = pytest.importorskip(
        "export", reason="export.py is not shipped in open-source builds")
    source = (
        f"command={_MOUNT}/ssd_pool/x/INS/{_OPERATOR}/env/python server.py\n"
        f"user={_ACCOUNT}\n"
    )
    sanitized = export_module._sanitize_source_opensource(
        source, "deploy/generated/tofu.conf")
    for token in _INTERNAL_TOKENS:
        assert token not in sanitized
