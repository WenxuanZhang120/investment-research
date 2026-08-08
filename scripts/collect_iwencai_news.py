#!/usr/bin/env python3
"""Search iWencai financial news and archive the unchanged response payload."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.save_raw_response import DEFAULT_RAW_ROOT, save_raw_response  # noqa: E402


API_URL = "https://openapi.iwencai.com/v1/comprehensive/search"
SKILL_ID = "news-search"
SKILL_VERSION = "1.0.0"
DEFAULT_SIZE = 10
DEFAULT_TIMEOUT = 30


class NewsCollectionError(RuntimeError):
    pass


def _api_key() -> str:
    value = os.environ.get("IWENCAI_API_KEY", "")
    if not value:
        raise NewsCollectionError("IWENCAI_API_KEY is not set")
    return value


def _request(*, query: str, size: int, timeout: int, api_key: str) -> Dict[str, Any]:
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(
            {
                "query": query,
                "channels": ["news"],
                "app_id": "AIME_SKILL",
                "size": size,
            },
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Claw-Call-Type": "normal",
            "X-Claw-Skill-Id": SKILL_ID,
            "X-Claw-Skill-Version": SKILL_VERSION,
            "X-Claw-Plugin-Id": "none",
            "X-Claw-Plugin-Version": "none",
            "X-Claw-Trace-Id": secrets.token_hex(32),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw_body = response.read()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise NewsCollectionError(
            f"iWencai HTTP {error.code}: {body or error.reason}"
        ) from error
    except urllib.error.URLError as error:
        raise NewsCollectionError(f"iWencai network error: {error.reason}") from error
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise NewsCollectionError("iWencai returned non-JSON data") from error
    if not isinstance(payload, dict):
        raise NewsCollectionError("iWencai response root must be an object")
    return payload


def collect_news(
    query: str,
    *,
    size: int = DEFAULT_SIZE,
    timeout: int = DEFAULT_TIMEOUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    request: Callable[..., Dict[str, Any]] = _request,
) -> Path:
    if not isinstance(query, str) or not query.strip():
        raise NewsCollectionError("query must be non-empty")
    if size < 1 or timeout < 1:
        raise NewsCollectionError("size and timeout must be positive")
    payload = request(
        query=query,
        size=size,
        timeout=timeout,
        api_key=_api_key(),
    )
    snapshot = save_raw_response(
        payload,
        source="iwencai",
        query=query,
        raw_root=raw_root,
    )
    if payload.get("status_code") != 0:
        raise NewsCollectionError(
            f"gateway status_code is not zero; response saved at {snapshot}"
        )
    if not isinstance(payload.get("data"), list):
        raise NewsCollectionError(
            f"gateway data is not an array; response saved at {snapshot}"
        )
    return snapshot


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Archive an iWencai financial-news search.")
    parser.add_argument("query")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    args = parser.parse_args(argv)
    try:
        destination = collect_news(
            args.query,
            size=args.size,
            timeout=args.timeout,
            raw_root=args.raw_root,
        )
    except (NewsCollectionError, OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
