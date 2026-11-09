"""The installable release cannot fall back to a public debug signing key."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _validator():
    spec = importlib.util.spec_from_file_location(
        'mobile_release_validation', ROOT / 'android/validate_release.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_missing_private_signing_is_actionable_and_fail_closed():
    with pytest.raises(ValueError, match='TOFU_ANDROID_KEYSTORE_PATH'):
        _validator().validate_signing_environment({})


@pytest.mark.parametrize('alias', ['androiddebugkey', 'AndroidDebugKey', 'ANDROIDDEBUGKEY'])
def test_debug_key_alias_cannot_be_a_release_identity(tmp_path, alias):
    key = tmp_path / 'private.jks'
    key.write_bytes(b'not-a-real-key')
    env = {
        'TOFU_ANDROID_KEYSTORE_PATH': str(key),
        'TOFU_ANDROID_KEYSTORE_PASSWORD': 'fixture-store-password',
        'TOFU_ANDROID_KEY_ALIAS': alias,
        'TOFU_ANDROID_KEY_PASSWORD': 'fixture-key-password',
        'TOFU_ANDROID_SIGNER_SHA256': 'a' * 64,
    }
    with pytest.raises(ValueError, match='debug'):
        _validator().validate_signing_environment(env)


def test_source_tag_must_match_the_build_version():
    validator = _validator()
    assert validator.validate_tag('android-v0.1.19', '0.1.19') is None
    with pytest.raises(ValueError, match='versionName'):
        validator.validate_tag('android-v0.1.18', '0.1.19')


@pytest.mark.parametrize('code,baseline', [(16, 16), (15, 16), (1, 16)])
def test_first_private_release_cannot_regress_the_published_build(code, baseline):
    with pytest.raises(ValueError, match='versionCode'):
        _validator().validate_version_code(code, baseline)


def test_private_release_may_advance_the_published_build():
    assert _validator().validate_version_code(20, 16) is None


def test_release_build_does_not_reference_debug_signing():
    text = (ROOT / 'android/app/build.gradle.kts').read_text()
    release = text.split('release {', 1)[1].split('compileOptions', 1)[0]
    assert 'signingConfigs.getByName("debug")' not in release
    assert 'signingConfigs.getByName("release")' in release
    assert 'applicationIdSuffix = ".debug"' in text


def test_release_workflow_stages_private_key_verifies_signer_and_always_cleans():
    workflow = yaml.safe_load((ROOT / '.github/workflows/build-android-apk.yml').read_text())
    steps = workflow['jobs']['build']['steps']
    stage = next(s for s in steps if s.get('id') == 'private_signing')
    assert 'TOFU_ANDROID_KEYSTORE_BASE64' in str(stage)
    verify = next(s for s in steps if s.get('name') == 'Verify release identity and artifact')
    assert 'validate_release.py' in verify['run']
    assert 'TOFU_ANDROID_SIGNER_SHA256' in str(verify)
    cleanup = next(s for s in steps if s.get('name') == 'Remove staged signing key')
    assert 'always()' in cleanup['if']


def test_relative_keystore_validation_uses_the_gradle_app_directory(monkeypatch, tmp_path):
    """CLI validation and Gradle agree on the environment path's base directory."""
    app = tmp_path / 'app'
    app.mkdir()
    (app / 'signer.jks').write_bytes(b'fixture-for-path-admission-only')
    script = tmp_path / 'validate_release.py'
    script.write_text((ROOT / 'android/validate_release.py').read_text())
    spec = importlib.util.spec_from_file_location('fixture_release_path_validation', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    environment = {
        'TOFU_ANDROID_KEYSTORE_PATH': 'signer.jks',
        'TOFU_ANDROID_KEYSTORE_PASSWORD': 'fixture-store-password',
        'TOFU_ANDROID_KEY_ALIAS': 'fixture-release',
        'TOFU_ANDROID_KEY_PASSWORD': 'fixture-key-password',
        'TOFU_ANDROID_SIGNER_SHA256': 'a' * 64,
    }
    for cwd in (tmp_path, app):
        monkeypatch.chdir(cwd)
        assert module.validate_signing_environment(environment) is None
