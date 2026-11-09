#!/usr/bin/env python3
"""Validate private Android release identity; never print signing secrets.

CI calls this before publishing, after apksigner verification. A successful
validation emits a publication receipt alongside the APK; operators promote
that receipt to published-release.json only after the release asset is live.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path
from collections.abc import Mapping

REQUIRED_SIGNING = (
    'TOFU_ANDROID_KEYSTORE_PATH', 'TOFU_ANDROID_KEYSTORE_PASSWORD',
    'TOFU_ANDROID_KEY_ALIAS', 'TOFU_ANDROID_KEY_PASSWORD',
    'TOFU_ANDROID_SIGNER_SHA256',
)


def validate_signing_environment(environ: Mapping[str, str]) -> None:
    missing = [name for name in REQUIRED_SIGNING if not environ.get(name, '').strip()]
    if missing:
        raise ValueError('Private release signing requires: ' + ', '.join(missing))
    key = Path(environ['TOFU_ANDROID_KEYSTORE_PATH'])
    if not key.is_absolute():
        key = Path(__file__).resolve().parent / 'app' / key
    if not key.is_file():
        raise ValueError('TOFU_ANDROID_KEYSTORE_PATH must name a readable private keystore')
    if key.name == 'debug.keystore' or environ['TOFU_ANDROID_KEY_ALIAS'].casefold() == 'androiddebugkey':
        raise ValueError('The public debug key cannot sign a release')
    fingerprint = re.sub(r'[^a-fA-F0-9]', '', environ['TOFU_ANDROID_SIGNER_SHA256']).lower()
    if len(fingerprint) != 64:
        raise ValueError('TOFU_ANDROID_SIGNER_SHA256 must be a SHA-256 certificate fingerprint')


def validate_tag(tag: str, version_name: str) -> None:
    if tag != 'android-v' + version_name:
        raise ValueError('android-v tag must match the Gradle versionName')


def validate_version_code(code: int, previous_code: int) -> None:
    if type(code) is not int or type(previous_code) is not int or code <= previous_code:
        raise ValueError('Release versionCode must strictly increase beyond the published build')


def parse_apk_identity(output: str) -> dict:
    match = re.search(r"package: name='([^']+)' versionCode='(\d+)' versionName='([^']+)'", output)
    if not match:
        raise ValueError('Could not read the APK manifest identity')
    return {'package': match.group(1), 'versionCode': int(match.group(2)), 'versionName': match.group(3)}


def validate_certificate(output: str, expected: str) -> None:
    expected = re.sub(r'[^a-fA-F0-9]', '', expected).lower()
    fingerprints = re.findall(r'Signer #\d+ certificate SHA-256 digest:\s*([a-fA-F0-9]+)', output)
    if len(fingerprints) != 1 or fingerprints[0].lower() != expected:
        raise ValueError('Release APK certificate does not match TOFU_ANDROID_SIGNER_SHA256')
    if re.search(r'Android Debug|CN=Android Debug', output, re.I):
        raise ValueError('The public debug certificate cannot sign a release')


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apk', type=Path, required=True)
    parser.add_argument('--apksigner', required=True)
    parser.add_argument('--aapt', required=True)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    try:
        signing_environment = {
            'TOFU_ANDROID_KEYSTORE_PATH': os.environ.get('TOFU_ANDROID_KEYSTORE_PATH', ''),
            'TOFU_ANDROID_KEYSTORE_PASSWORD': os.environ.get('TOFU_ANDROID_KEYSTORE_PASSWORD', ''),
            'TOFU_ANDROID_KEY_ALIAS': os.environ.get('TOFU_ANDROID_KEY_ALIAS', ''),
            'TOFU_ANDROID_KEY_PASSWORD': os.environ.get('TOFU_ANDROID_KEY_PASSWORD', ''),
            'TOFU_ANDROID_SIGNER_SHA256': os.environ.get('TOFU_ANDROID_SIGNER_SHA256', ''),
        }
        validate_signing_environment(signing_environment)
        gradle = Path(__file__).resolve().parent / 'app/build.gradle.kts'
        source = gradle.read_text(encoding='utf-8')
        version = re.search(r'versionName\s*=\s*"([^"]+)"', source).group(1)
        code = int(re.search(r'versionCode\s*=\s*(\d+)', source).group(1))
        validate_tag(args.tag, version)
        published = json.loads(gradle.parents[1].joinpath('published-release.json').read_text(encoding='utf-8'))
        validate_version_code(code, published.get('versionCode'))
        verified = subprocess.run([args.apksigner, 'verify', '--verbose', '--print-certs', str(args.apk)],
                                  check=True, capture_output=True, text=True)
        validate_certificate(verified.stdout, signing_environment['TOFU_ANDROID_SIGNER_SHA256'])
        badging = subprocess.run([args.aapt, 'dump', 'badging', str(args.apk)],
                                check=True, capture_output=True, text=True)
        identity = parse_apk_identity(badging.stdout)
        if identity != {'package': 'com.tofu.client', 'versionCode': code, 'versionName': version}:
            raise ValueError('APK manifest differs from the release source identity')
        if 'application-debuggable' in badging.stdout:
            raise ValueError('A debuggable APK cannot be published as release')
        receipt = {
            'schemaVersion': 1, 'repository': 'rangehow/tofu-android', 'tag': args.tag,
            'versionName': version, 'versionCode': code, 'filename': 'tofu-android.apk',
            'url': f'https://github.com/rangehow/tofu-android/releases/download/{args.tag}/tofu-android.apk',
            'sha256': hashlib.sha256(args.apk.read_bytes()).hexdigest(),
            'sizeBytes': args.apk.stat().st_size,
        }
        args.receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
    except (ValueError, OSError, AttributeError, subprocess.CalledProcessError) as error:
        # Tool output and credential values are deliberately not included.
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        parser.exit(1, 'Release validation failed: ' + reason + '\n')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
