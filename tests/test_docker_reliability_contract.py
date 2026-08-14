"""Static guardrails for the production Docker lifecycle contract."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def test_compose_has_bounded_self_healing_runtime():
    text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    for contract in (
        'restart: unless-stopped',
        'init: true',
        'mem_limit:',
        'mem_reservation:',
        'pids_limit:',
        'stop_grace_period: 45s',
        'http://localhost:15000/api/health',
        'max-size:',
        'max-file:',
        '${TOFU_BACKUP_VOLUME:-tofu-backups}:/app/data/backups',
    ):
        assert contract in text, f'missing Docker reliability contract: {contract}'


def test_image_healthcheck_uses_liveness_endpoint():
    text = (ROOT / 'Dockerfile').read_text(encoding='utf-8')
    assert 'HEALTHCHECK ' in text
    assert 'http://localhost:15000/api/health' in text
