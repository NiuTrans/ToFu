"""Site-packages normalization that keeps the payload free of build-host state."""
from runtime_builder.payload import drop_build_host_metadata


def test_console_scripts_and_uv_cache_records_are_dropped(tmp_path):
    site = tmp_path / 'site'
    info = site / 'demo-1.0.dist-info'
    info.mkdir(parents=True)
    (site / 'bin').mkdir()
    (site / 'bin' / 'demo').write_text('#!/build/host/python3\n')
    (site / 'demo').mkdir()
    (site / 'demo' / '__init__.py').write_text('')
    (info / 'uv_cache.json').write_text('{"timestamp": 1}')
    (info / 'RECORD').write_text(
        'bin/demo,sha256=abc,22\n'
        'demo/__init__.py,sha256=def,0\n'
        'demo-1.0.dist-info/uv_cache.json,sha256=ghi,16\n'
        'demo-1.0.dist-info/RECORD,,\n')
    untouched = site / 'other-2.0.dist-info'
    untouched.mkdir()
    (untouched / 'RECORD').write_text('other/__init__.py,sha256=x,0\nother-2.0.dist-info/RECORD,,\n')

    drop_build_host_metadata(site)

    assert not (site / 'bin').exists()
    assert not (info / 'uv_cache.json').exists()
    assert (info / 'RECORD').read_text() == (
        'demo/__init__.py,sha256=def,0\n'
        'demo-1.0.dist-info/RECORD,,\n')
    assert (untouched / 'RECORD').read_text() == 'other/__init__.py,sha256=x,0\nother-2.0.dist-info/RECORD,,\n'
