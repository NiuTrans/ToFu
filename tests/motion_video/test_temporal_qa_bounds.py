"""Invalid or cancelled capture requests cannot allocate a browser or overwrite evidence."""

import pytest

from lib.design_sys.temporal_qa import screenshot_timeline_contact_sheet

pytestmark = pytest.mark.unit


@pytest.mark.parametrize('points', [[], [float('nan')], [float('inf')], [-0.1], [1.1], [0.5] * 13])
def test_invalid_samples_are_rejected_before_capture(tmp_path, points):
    (tmp_path / 'index.html').write_text('<div data-duration="10"></div>')
    with pytest.raises(ValueError, match='progress|samples|render duration'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(tmp_path / 'sheet.png'),
                                          width=100, height=100, progresses=points)


@pytest.mark.parametrize('duration', [0, -1, float('nan'), float('inf'), 3601])
def test_invalid_render_duration_is_rejected(tmp_path, duration):
    (tmp_path / 'index.html').write_text('<div data-duration="10"></div>')
    with pytest.raises(ValueError, match='render duration'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(tmp_path / 'sheet.png'),
                                          width=100, height=100, duration_s=duration)


def test_cancel_preserves_previous_evidence(tmp_path):
    (tmp_path / 'index.html').write_text('<div data-duration="10"></div>')
    output = tmp_path / 'sheet.png'
    output.write_bytes(b'previous evidence')
    with pytest.raises(InterruptedError, match='aborted'):
        screenshot_timeline_contact_sheet(str(tmp_path), str(output), width=100, height=100,
                                          abort_check=lambda: True)
    assert output.read_bytes() == b'previous evidence'
