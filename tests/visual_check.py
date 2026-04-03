"""VLM-powered screenshot analysis for visual regression testing.

Uses the project's own LLM client to analyze screenshots and detect UI anomalies.
Falls back to rule-based checks when VLM is unavailable.

Usage:
    from tests.visual_check import analyze_screenshot, VisualVerdict

    verdict = analyze_screenshot(
        screenshot_path="tests/screenshots/chat_normal.png",
        check_description="Normal chat mode after sending a message",
        expected_elements=["sidebar", "input box", "assistant message", "avatar"],
    )
    assert verdict.ok, verdict.summary
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from dataclasses import dataclass, field

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


@dataclass
class VisualVerdict:
    """Result of a visual analysis check."""
    ok: bool
    summary: str
    issues: list[str] = field(default_factory=list)
    elements_found: list[str] = field(default_factory=list)
    elements_missing: list[str] = field(default_factory=list)
    raw_response: str = ""

    def __str__(self):
        status = "✅ PASS" if self.ok else "❌ FAIL"
        parts = [f"{status}: {self.summary}"]
        if self.issues:
            parts.append(f"  Issues: {'; '.join(self.issues)}")
        if self.elements_missing:
            parts.append(f"  Missing: {', '.join(self.elements_missing)}")
        return "\n".join(parts)


# ── VLM-based analysis (primary) ────────────────────────────────────

_VLM_PROMPT_TEMPLATE = """You are a UI quality assurance expert reviewing a screenshot of a web-based AI chat application called "Tofu".

**Task**: Analyze this screenshot and report any visual anomalies or layout issues.

**Check description**: {check_description}

**Expected elements** (verify each is present and properly rendered):
{expected_elements_list}

**Common issues to look for**:
1. Layout broken — elements overlapping, overflowing, or misaligned
2. Text rendering — garbled text, wrong encoding, truncated content
3. Sidebar — conversation list should be clean, items properly spaced
4. Input area — text box, send button, tool toggles should be visible and well-arranged
5. Message bubbles — proper avatar, role label, formatted content (markdown, code blocks)
6. Empty states — blank areas where content should be
7. Dark theme consistency — no white flash, contrast issues
8. Responsive layout — nothing cut off or hidden incorrectly

**Response format** (respond in EXACTLY this JSON format):
```json
{{
  "ok": true/false,
  "summary": "One-line overall assessment",
  "elements_found": ["element1", "element2"],
  "elements_missing": ["element3"],
  "issues": ["description of issue 1", "description of issue 2"]
}}
```

If everything looks good, set "ok": true and "issues": [].
If there are visual problems, set "ok": false and describe each issue clearly.
"""


def analyze_screenshot(
    screenshot_path: str,
    check_description: str,
    expected_elements: list[str] | None = None,
    *,
    vlm_model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> VisualVerdict:
    """Analyze a screenshot using VLM and return a structured verdict.

    Args:
        screenshot_path: Path to the PNG screenshot.
        check_description: What this screenshot is supposed to show.
        expected_elements: List of UI elements that should be visible.
        vlm_model: Model to use (default: gemini flash lite or env override).
        api_key: API key (default: from env / lib config).
        base_url: API base URL (default: from env / lib config).
        timeout: Request timeout in seconds.

    Returns:
        VisualVerdict with analysis results.
    """
    expected_elements = expected_elements or []

    # Try VLM analysis first
    try:
        return _vlm_analyze(
            screenshot_path, check_description, expected_elements,
            vlm_model=vlm_model, api_key=api_key, base_url=base_url,
            timeout=timeout,
        )
    except Exception as e:
        print(f"  ⚠️  VLM analysis failed ({e}), falling back to rule-based check")
        return _rule_based_analyze(screenshot_path, check_description, expected_elements)


def _vlm_analyze(
    screenshot_path: str,
    check_description: str,
    expected_elements: list[str],
    *,
    vlm_model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float = 30.0,
) -> VisualVerdict:
    """Use VLM to analyze the screenshot."""
    import requests

    # Read and encode the screenshot
    with open(screenshot_path, "rb") as f:
        img_data = base64.b64encode(f.read()).decode("utf-8")

    # Config: use env vars or project defaults
    if not api_key:
        api_key = os.environ.get("VLM_API_KEY") or os.environ.get("LLM_API_KEY", "")
        if not api_key:
            try:
                from lib import LLM_API_KEY
                api_key = LLM_API_KEY
            except ImportError:
                pass

    if not base_url:
        base_url = os.environ.get("VLM_BASE_URL") or os.environ.get("LLM_BASE_URL", "")
        if not base_url:
            try:
                from lib import LLM_BASE_URL
                base_url = LLM_BASE_URL
            except ImportError:
                pass

    if not vlm_model:
        vlm_model = os.environ.get("VLM_MODEL", "gemini-2.5-flash-lite-preview")

    if not api_key or not base_url:
        raise RuntimeError("No VLM API key or base URL available")

    # Build prompt
    elements_list = "\n".join(f"  - {e}" for e in expected_elements) or "  (no specific elements listed)"
    prompt = _VLM_PROMPT_TEMPLATE.format(
        check_description=check_description,
        expected_elements_list=elements_list,
    )

    # Call VLM
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = {
        "model": vlm_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{img_data}"}
                },
            ],
        }],
        "max_tokens": 1024,
        "temperature": 0,
    }

    resp = requests.post(url, headers=headers, json=body, timeout=timeout,
                         proxies={"http": None, "https": None})
    resp.raise_for_status()
    data = resp.json()

    raw_text = data["choices"][0]["message"]["content"]

    # Parse JSON from response
    return _parse_vlm_response(raw_text, expected_elements)


def _parse_vlm_response(raw_text: str, expected_elements: list[str]) -> VisualVerdict:
    """Parse the VLM JSON response into a VisualVerdict."""
    # Extract JSON from markdown code block if present
    json_match = re.search(r"```json\s*\n?(.*?)\n?\s*```", raw_text, re.DOTALL)
    if json_match:
        json_str = json_match.group(1)
    else:
        # Try to find raw JSON
        json_match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        json_str = json_match.group(0) if json_match else raw_text

    try:
        result = json.loads(json_str)
    except json.JSONDecodeError:
        return VisualVerdict(
            ok=False,
            summary="Failed to parse VLM response as JSON",
            issues=[f"Raw response: {raw_text[:500]}"],
            raw_response=raw_text,
        )

    return VisualVerdict(
        ok=result.get("ok", False),
        summary=result.get("summary", "No summary"),
        issues=result.get("issues", []),
        elements_found=result.get("elements_found", []),
        elements_missing=result.get("elements_missing", []),
        raw_response=raw_text,
    )


# ── Rule-based fallback (when VLM is unavailable) ───────────────────

def _rule_based_analyze(
    screenshot_path: str,
    check_description: str,
    expected_elements: list[str],
) -> VisualVerdict:
    """Basic rule-based screenshot analysis using file size and dimensions.

    This is a fallback when VLM is unavailable. It can detect:
    - Blank/empty screenshots (file too small)
    - Extremely large screenshots (possible rendering explosion)
    - Valid screenshots (at least passes the basics)
    """
    issues = []

    # Check file exists and has reasonable size
    if not os.path.exists(screenshot_path):
        return VisualVerdict(
            ok=False,
            summary="Screenshot file does not exist",
            issues=[f"File not found: {screenshot_path}"],
        )

    file_size = os.path.getsize(screenshot_path)

    # A blank white/black page is typically <5KB for 1280x800
    if file_size < 3000:
        issues.append(f"Screenshot suspiciously small ({file_size} bytes) — possibly blank page")

    # An extremely large screenshot might indicate rendering issues
    if file_size > 10_000_000:
        issues.append(f"Screenshot very large ({file_size/1e6:.1f}MB) — possible rendering explosion")

    # Try to get image dimensions via PIL if available
    try:
        from PIL import Image
        img = Image.open(screenshot_path)
        width, height = img.size

        # Check for reasonable dimensions
        if width < 100 or height < 100:
            issues.append(f"Screenshot too small: {width}x{height}")

        # Check for mostly-single-color (blank) image
        # Sample some pixels
        pixels = list(img.getdata())
        if len(set(pixels[:1000])) < 5:
            issues.append("Screenshot appears to be mostly a single color (blank/error page)")

        img.close()
    except ImportError:
        pass  # PIL not available, skip pixel analysis
    except Exception as e:
        issues.append(f"Could not analyze image: {e}")

    ok = len(issues) == 0
    return VisualVerdict(
        ok=ok,
        summary="Rule-based check passed (VLM unavailable)" if ok else "Rule-based check found issues",
        issues=issues,
        elements_found=[],
        elements_missing=expected_elements,  # can't verify without VLM
    )
