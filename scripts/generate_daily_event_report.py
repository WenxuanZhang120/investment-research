#!/usr/bin/env python3
"""Generate a factual daily announcement index from normalized event bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


EVENT_TYPE_LABELS = {
    "periodic_report": "定期报告",
    "earnings_forecast": "业绩预告/快报",
    "dividend": "分红派息",
    "share_repurchase": "股份回购",
    "shareholder_change": "股东增减持",
    "restructuring": "资产重组",
    "regulatory": "监管事项",
    "pledge": "股权质押",
    "unlock": "限售解禁",
    "other_announcement": "其他公告",
}


class EventReportError(ValueError):
    pass


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise EventReportError("event row must be an object")
                yield value


def load_events(manifests: Sequence[Path]) -> List[Dict[str, Any]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    for manifest_path in manifests:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        table = manifest.get("table")
        if not isinstance(table, dict) or table.get("logical_name") != "events":
            raise EventReportError(f"not an event manifest: {manifest_path}")
        event_path = Path(manifest_path).parent / table["file"]
        if hashlib.sha256(event_path.read_bytes()).hexdigest() != table.get("sha256"):
            raise EventReportError(f"event hash mismatch: {event_path}")
        for event in _iter_jsonl(event_path):
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or not event_id:
                raise EventReportError("event_id is required")
            existing = by_id.get(event_id)
            if existing is None or event["fetched_at"] > existing["fetched_at"]:
                by_id[event_id] = event
    return sorted(
        by_id.values(),
        key=lambda item: (item["published_at"], item["event_id"]),
        reverse=True,
    )


def render_report(events: Sequence[Dict[str, Any]], report_date: str) -> str:
    counts = Counter(event["event_type"] for event in events)
    lines = [
        f"# {report_date} 公告事件日报",
        "",
        "本报告是公告事实索引，不构成利好/利空判断或投资建议。",
        "",
        "## 摘要",
        "",
        f"- 公告数量：{len(events)}",
    ]
    for event_type, count in sorted(counts.items()):
        lines.append(f"- {EVENT_TYPE_LABELS.get(event_type, event_type)}：{count}")
    lines.extend(["", "## 公告明细", ""])
    if not events:
        lines.append("当日输入批次没有公告。")
    for event in events:
        name = event.get("security_name") or event.get("source_security_code") or "未关联证券"
        lines.extend(
            [
                f"### {name}｜{EVENT_TYPE_LABELS.get(event['event_type'], event['event_type'])}",
                "",
                f"- 标题：[{event['title']}]({event['url']})",
                f"- 发布时间：{event['published_at']}",
                f"- 来源：同花顺问财",
                f"- 来源记录：`{event['raw_record_id']}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a factual daily event report.")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        content = render_report(load_events(args.manifests), args.date)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(content)
    except (OSError, TypeError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
