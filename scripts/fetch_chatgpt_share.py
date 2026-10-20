#!/usr/bin/env python3
"""Export a public ChatGPT share link to Markdown without logging in.

ChatGPT share pages (`https://chatgpt.com/s/<id>`) are not server-rendered: the
HTML contains no transcript, and stripping tags yields only the page chrome.
The transcript arrives as a React Router "turbo-stream" payload emitted by
inline scripts as `window.__reactRouterContext.streamController.enqueue("<json
string literal>")`. Concatenating those chunks yields the flight data, in which
each message body appears verbatim as a JSON string literal next to its role:

    "parts",[1912],"<message markdown>","role","assistant"

That `parts`/`role` adjacency is the extraction anchor. A length heuristic
covers share pages serialized without it, at the cost of losing the role.

Usage:
    python3 scripts/fetch_chatgpt_share.py <url-or-share-id> [--out PATH]

Exit codes: 0 exported, 1 fetch/decode failure, 2 no transcript found.
"""

from __future__ import annotations

import argparse
import json
import re
import sys

import requests

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126 Safari/537.36"
)

ENQUEUE_PATTERN = re.compile(
    r"streamController\.enqueue\(\s*(\"(?:[^\"\\]|\\.)*\")\s*\)", re.S
)
MESSAGE_PATTERN = re.compile(
    r"\"parts\",\[\d+\],(\"(?:[^\"\\]|\\.)*\"),\"role\",\"([a-z]+)\"", re.S
)
STRING_LITERAL_PATTERN = re.compile(r"\"((?:[^\"\\]|\\.)*)\"", re.S)
# ChatGPT injects inline citation widgets as private-use markers around an
# opaque JSON payload; the visible marker text lives in a sibling string, so
# the block has to be dropped from the exported Markdown.
CITATION_PATTERN = re.compile(r"[\ue200][^\ue201]*[\ue201]")
TRACKING_SUFFIX_PATTERN = re.compile(r"[?&]utm_source=[^)\s]*")
URL_PATTERN = re.compile(r"\((https?://[^\s)]+)\)")
MIN_BODY_CHARS = 200


def normalize_url(value: str) -> str:
    if value.startswith(("http://", "https://")):
        return value
    share_id = value.strip().strip("/")
    return f"https://chatgpt.com/s/{share_id}"


def fetch_html(url: str, timeout: float) -> str:
    # requests (not urllib) because urllib's CONNECT tunnel hangs indefinitely
    # through the corporate HTTP proxy on this network.
    response = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    return response.text


def decode_flight(html: str) -> str:
    chunks = []
    for match in ENQUEUE_PATTERN.finditer(html):
        try:
            chunks.append(json.loads(match.group(1)))
        except json.JSONDecodeError:
            continue
    return "".join(chunks)


def _dedupe(messages: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Drop repeats: the flight table re-serializes a reference's value."""
    seen: set[str] = set()
    unique = []
    for role, body in messages:
        if body not in seen:
            seen.add(body)
            unique.append((role, body))
    return unique


def extract_messages(flight: str) -> list[tuple[str, str]]:
    """Return `(role, markdown)` pairs in page order."""
    anchored = []
    for match in MESSAGE_PATTERN.finditer(flight):
        try:
            body = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if body.strip():
            anchored.append((match.group(2), body))
    if anchored:
        return _dedupe(anchored)

    fallback = []
    for match in STRING_LITERAL_PATTERN.finditer(flight):
        try:
            body = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        if len(body) >= MIN_BODY_CHARS and "\n" in body:
            fallback.append(("unknown", body))
    return _dedupe(fallback)


def _clean(body: str) -> str:
    body = CITATION_PATTERN.sub("", body)
    body = TRACKING_SUFFIX_PATTERN.sub("", body)
    return body.strip()


def render(messages: list[tuple[str, str]], url: str) -> str:
    sections = [f"> 来源：{url}\n"]
    for index, (role, body) in enumerate(messages, start=1):
        cleaned = _clean(body)
        if len(messages) > 1:
            sections.append(f"## 消息 {index} · {role}\n\n{cleaned}")
        else:
            sections.append(cleaned)

    links: list[str] = []
    for _, body in messages:
        for link in URL_PATTERN.findall(_clean(body)):
            if link not in links:
                links.append(link)
    if links:
        sections.append("## 链接\n\n" + "\n".join(f"- {link}" for link in links))
    return "\n\n---\n\n".join(sections) + "\n"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", help="Share URL or bare share id")
    parser.add_argument("--out", help="Write Markdown here instead of stdout")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    url = normalize_url(args.target)
    try:
        html = fetch_html(url, args.timeout)
    except (requests.RequestException, OSError) as error:
        print(f"fetch failed: {url}: {error}", file=sys.stderr)
        return 1

    flight = decode_flight(html)
    if not flight:
        print(
            "no React Router flight payload found; the link is probably not a "
            "public share or the page format changed",
            file=sys.stderr,
        )
        return 1

    messages = extract_messages(flight)
    if not messages:
        print("flight payload found but no message bodies matched", file=sys.stderr)
        return 2

    markdown = render(messages, url)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as handle:
                handle.write(markdown)
        except OSError as error:
            print(f"cannot write {args.out}: {error}", file=sys.stderr)
            return 1
        print(f"{len(messages)} message(s), {len(markdown)} chars -> {args.out}")
    else:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
