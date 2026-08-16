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
from scripts.normalize_iwencai_financials import financial_period_evidence  # noqa: E402
from scripts.public_payload_safety import PublicPayloadSafetyError  # noqa: E402


COLLECTOR_VERSION = "1.3.0"
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
    if response.get("success") is False:
        raise CollectionError("iWencai response explicitly reported success=false")
    status_code = response.get("status_code")
    if status_code is not None and status_code != 0:
        raise CollectionError(
            f"iWencai response explicitly reported status_code={status_code}"
        )
    datas = response.get("datas")
    if not isinstance(datas, list):
        raise CollectionError("iWencai response does not contain a datas array")
    try:
        code_count = int(response.get("code_count", 0))
    except (TypeError, ValueError) as error:
        raise CollectionError("iWencai code_count must be an integer") from error
    if code_count < 0:
        raise CollectionError("iWencai code_count cannot be negative")
    if "claw_headers" in response:
        raise CollectionError(
            "iWencai response unexpectedly contains credential-bearing claw_headers"
        )

    # Request pagination is provenance, not part of the provider response.
    # Preserve the provider response exactly and persist page/limit in metadata.
    return response


def _response_field_names(response: Dict[str, Any]) -> List[str]:
    fields = set()
    columns = response.get("columns")
    if isinstance(columns, list):
        for column in columns:
            if isinstance(column, dict):
                name = column.get("key") or column.get("index_name")
                if isinstance(name, str) and name:
                    fields.add(name)
    rows = response.get("datas")
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict):
                fields.update(name for name in row if isinstance(name, str) and name)
    return sorted(fields)


def _validate_collection_period(
    response: Dict[str, Any], collection_job: Optional[Dict[str, Any]]
) -> None:
    if collection_job is None:
        return
    expected_period = collection_job.get("expected_period_end")
    evidence = financial_period_evidence(_response_field_names(response))
    expected_only = [expected_period]
    for name in (
        "financial_periods",
        "report_period_label_periods",
        "filing_date_periods",
    ):
        if evidence[name] != expected_only:
            raise CollectionError(
                "iWencai financial period contract mismatch after Raw save: "
                f"{name} expected {expected_only}, found {evidence[name]}"
            )


def collect_query(
    query: str,
    *,
    start_page: int = 1,
    limit: int = DEFAULT_LIMIT,
    max_pages: int = DEFAULT_MAX_PAGES,
    page_budget: Optional[int] = None,
    timeout: int = DEFAULT_TIMEOUT,
    raw_root: Path = DEFAULT_RAW_ROOT,
    request_page: Callable[..., Dict[str, Any]] = _request_page,
    collection_job: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Collect a complete query or a validated continuation page segment."""
    if not isinstance(query, str) or not query.strip():
        raise CollectionError("query must be a non-empty string")
    if start_page < 1 or limit < 1 or max_pages < 1 or timeout < 1:
        raise CollectionError(
            "start_page, limit, max_pages, and timeout must be positive"
        )
    if page_budget is not None and page_budget < 1:
        raise CollectionError("page_budget must be positive when supplied")
    if start_page > max_pages:
        raise CollectionError("start_page cannot exceed max_pages")

    api_key = _api_key()
    snapshot_paths: List[Path] = []
    expected_total: Optional[int] = None
    seen_security_codes = set()
    collected_rows = 0
    expected_page_count: Optional[int] = None

    budget_end_page = (
        min(max_pages, start_page + page_budget - 1)
        if page_budget is not None
        else max_pages
    )
    reached_query_end = False
    for page in range(start_page, budget_end_page + 1):
        response = _request_with_retry(
            request_page,
            query=query,
            page=page,
            limit=limit,
            api_key=api_key,
            timeout=timeout,
        )
        if "claw_headers" in response:
            raise CollectionError(
                "iWencai response unexpectedly contains credential-bearing claw_headers"
            )
        try:
            snapshot_path = save_raw_response(
                response,
                source="iwencai",
                query=query,
                raw_root=raw_root,
                collection_job=collection_job,
                collection_request={
                    "request_schema_version": 1,
                    "page": page,
                    "limit": limit,
                },
            )
        except PublicPayloadSafetyError as error:
            raise CollectionError(str(error)) from error
        snapshot_paths.append(snapshot_path)
        payload = _safe_response_payload(
            response,
            query=query,
            page=page,
            limit=limit,
        )
        _validate_collection_period(payload, collection_job)

        total = int(payload["code_count"])
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
        if page * limit >= total:
            reached_query_end = True
            break
    if not reached_query_end and not (
        page_budget is not None
        and budget_end_page == start_page + page_budget - 1
        and expected_page_count is not None
        and budget_end_page < expected_page_count
    ):
        raise CollectionError(
            f"query requires more than the configured max_pages={max_pages}"
        )

    if expected_total is None or expected_page_count is None:
        raise CollectionError("query did not return pagination metadata")
    segment_end_page = start_page + len(snapshot_paths) - 1
    expected_segment_pages = segment_end_page - start_page + 1
    expected_segment_rows = max(
        0,
        min(expected_total, segment_end_page * limit)
        - (start_page - 1) * limit,
    )
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
        "end_page": segment_end_page,
        "complete_query": start_page == 1 and reached_query_end,
        "reached_query_end": reached_query_end,
        "remaining_page_count": max(0, expected_page_count - segment_end_page),
        "next_page": None if reached_query_end else segment_end_page + 1,
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
    parser.add_argument(
        "--page-budget",
        type=int,
        help="Maximum number of pages to save in this invocation",
    )
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
            page_budget=args.page_budget,
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
