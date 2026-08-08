#!/usr/bin/env python3
"""Collect complete paginated iWencai financial responses into raw storage."""

from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.save_raw_response import DEFAULT_RAW_ROOT, save_raw_response  # noqa: E402


COLLECTOR_VERSION = "1.2.0"
API_URL = "https://openapi.iwencai.com/v1/query2data"
SKILL_ID = "hithink-finance-query"
SKILL_VERSION = "1.0.0"
DEFAULT_LIMIT = 100
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_PAGES = 200
MAX_RETRIES = 2


class CollectionError(RuntimeError):
    """Raised when a complete, auditable query cannot be collected."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


def _api_key() -> str:
    value = os.environ.get("IWENCAI_API_KEY", "")
    if not value:
        raise CollectionError(
            "IWENCAI_API_KEY is not set; obtain it from https://www.iwencai.com/skillhub"
        )
    return value


def _request_page(
    *,
    query: str,
    page: int,
    limit: int,
    api_key: str,
    timeout: int,
    call_type: str,
) -> Dict[str, Any]:
    trace_id = secrets.token_hex(32)
    request_payload = {
        "query": query,
        "page": str(page),
        "limit": str(limit),
        "is_cache": "1",
        "expand_index": "true",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Claw-Call-Type": call_type,
        "X-Claw-Skill-Id": SKILL_ID,
        "X-Claw-Skill-Version": SKILL_VERSION,
        "X-Claw-Plugin-Id": "none",
        "X-Claw-Plugin-Version": "none",
        "X-Claw-Trace-Id": trace_id,
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(request_payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8") if error.fp else ""
        raise CollectionError(
            f"iWencai HTTP {error.code}: {body or error.reason}",
            retryable=error.code == 429 or error.code >= 500,
        ) from error
    except urllib.error.URLError as error:
        raise CollectionError(
            f"iWencai network error: {error.reason}", retryable=True
        ) from error

    try:
        parsed = json.loads(response_body)
    except json.JSONDecodeError as error:
        raise CollectionError("iWencai returned a non-JSON response") from error
    if not isinstance(parsed, dict):
        raise CollectionError("iWencai response root must be an object")
    parsed = dict(parsed)
    parsed.pop("claw_headers", None)
    parsed["trace_id"] = trace_id
    return parsed


def _request_with_retry(
    request_page: Callable[..., Dict[str, Any]],
    **kwargs: Any,
) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return request_page(
                **kwargs,
                call_type="normal" if attempt == 0 else "retry",
            )
        except CollectionError as error:
            last_error = error
            if attempt < MAX_RETRIES and error.retryable:
                time.sleep(attempt + 1)
            else:
                raise
    raise CollectionError(f"iWencai request failed after retries: {last_error}")


def _safe_response_payload(
    response: Dict[str, Any],
    *,
    query: str,
    page: int,
    limit: int,
) -> Dict[str, Any]:
    datas = response.get("datas")
    if not isinstance(datas, list):
        raise CollectionError("iWencai response does not contain a datas array")
    try:
        code_count = int(response.get("code_count", 0))
    except (TypeError, ValueError) as error:
        raise CollectionError("iWencai code_count must be an integer") from error
    if code_count < 0:
        raise CollectionError("iWencai code_count cannot be negative")

    payload = dict(response)
    payload.pop("claw_headers", None)
    payload.update(
        {
            "success": True,
            "query": query,
            "code_count": code_count,
            "returned_count": len(datas),
            "page": str(page),
            "limit": str(limit),
            "has_more": page * limit < code_count,
            "datas": datas,
            "collector_version": COLLECTOR_VERSION,
        }
    )
    return payload


def collect_query(
    query: str,
    *,
    start_page: int = 1,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    timeout: int = DEFAULT_TIMEOUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    request_page: Callable[..., Dict[str, Any]] = _request_page,
) -> Dict[str, Any]:
    """Collect a complete query or a validated continuation page segment."""
    if not isinstance(query, str) or not query.strip():
        raise CollectionError("query must be a non-empty string")
    if start_page < 1 or limit < 1 or max_pages < 1 or timeout < 1:
        raise CollectionError(
            "start_page, limit, max_pages, and timeout must be positive"
        )
    if start_page > max_pages:
        raise CollectionError("start_page cannot exceed max_pages")

    api_key = _api_key()
    snapshot_paths: List[Path] = []
    expected_total: Optional[int] = None
    seen_security_codes = set()
    collected_rows = 0
    expected_page_count: Optional[int] = None

    for page in range(start_page, max_pages + 1):
        response = _request_with_retry(
            request_page,
            query=query,
            page=page,
            limit=limit,
            api_key=api_key,
            timeout=timeout,
        )
        payload = _safe_response_payload(
            response,
            query=query,
            page=page,
            limit=limit,
        )
        snapshot_path = save_raw_response(
            payload,
            source="iwencai",
            query=query,
            raw_root=raw_root,
        )
        snapshot_paths.append(snapshot_path)

        total = payload["code_count"]
        rows = payload["datas"]
        if expected_total is None:
            expected_total = total
            expected_page_count = max(1, math.ceil(total / limit))
            if start_page > expected_page_count:
                raise CollectionError(
                    f"start_page={start_page} exceeds query page count "
                    f"{expected_page_count}"
                )
        elif total != expected_total:
            raise CollectionError(
                f"code_count changed from {expected_total} to {total} on page {page}"
            )

        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                raise CollectionError(f"page {page} row {row_index} is not an object")
            security_code = row.get("股票代码")
            if not isinstance(security_code, str) or not security_code:
                raise CollectionError(
                    f"page {page} row {row_index} is missing 股票代码"
                )
            if security_code in seen_security_codes:
                raise CollectionError(
                    f"duplicate security code across pages: {security_code}"
                )
            seen_security_codes.add(security_code)

        collected_rows += len(rows)
        print(
            f"saved page {page}/{expected_page_count}: "
            f"{len(rows)} rows, cumulative {collected_rows}/{expected_total}",
            file=sys.stderr,
            flush=True,
        )
        if not payload["has_more"]:
            break
    else:
        raise CollectionError(
            f"query requires more than the configured max_pages={max_pages}"
        )

    if expected_total is None or expected_page_count is None:
        raise CollectionError("query did not return pagination metadata")
    expected_segment_pages = expected_page_count - start_page + 1
    expected_segment_rows = max(0, expected_total - (start_page - 1) * limit)
    if len(snapshot_paths) != expected_segment_pages:
        raise CollectionError(
            f"expected {expected_segment_pages} pages in segment, "
            f"collected {len(snapshot_paths)}"
        )
    if collected_rows != expected_segment_rows:
        raise CollectionError(
            f"expected {expected_segment_rows} rows in segment, "
            f"collected {collected_rows}"
        )

    return {
        "query": query,
        "source": "iwencai",
        "collector_version": COLLECTOR_VERSION,
        "limit": limit,
        "start_page": start_page,
        "end_page": expected_page_count,
        "complete_query": start_page == 1,
        "query_record_count": expected_total,
        "page_count": len(snapshot_paths),
        "record_count": collected_rows,
        "snapshot_paths": [str(path) for path in snapshot_paths],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect a complete paginated iWencai financial query."
    )
    parser.add_argument("--query", required=True, help="Natural-language query")
    parser.add_argument(
        "--start-page",
        type=int,
        default=1,
        help="First page to fetch when resuming an incomplete immutable collection",
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = collect_query(
            args.query,
            start_page=args.start_page,
            limit=args.limit,
            max_pages=args.max_pages,
            timeout=args.timeout,
            raw_root=args.raw_root,
        )
    except (OSError, TypeError, ValueError, CollectionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
