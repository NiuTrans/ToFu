"""Release APKs must run R8 without renaming JS→native bridge methods."""

from __future__ import annotations

from pathlib import Path

import pytest


pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_GRADLE = _ROOT / "android" / "app" / "build.gradle.kts"
_PROGUARD = _ROOT / "android" / "app" / "proguard-rules.pro"


def test_release_build_enables_r8_and_resource_shrinking():
    text = _GRADLE.read_text(encoding="utf-8")
    release = text.split("release {", 1)[1].split("compileOptions", 1)[0]
    assert "isMinifyEnabled = true" in release
    assert "isShrinkResources = true" in release
    assert "isMinifyEnabled = false" not in release


def test_proguard_keeps_javascript_interface_methods():
    text = _PROGUARD.read_text(encoding="utf-8")
    assert "@android.webkit.JavascriptInterface" in text
    assert "-keepclassmembers class *" in text
    assert "DISABLED" not in text
    assert "com.tofu.client.data" in text
