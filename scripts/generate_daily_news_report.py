#!/usr/bin/env python3
"""Generate a factual daily news index from normalized news bundles."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


class NewsReportError(ValueError):
    pass


def load_news(manifests: Sequence[Path]) -> List[Dict[str, Any]]:
    by_id = {}
    for manifest_path in manifests:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        table = manifest.get("table")
        if not isinstance(table, dict) or table.get("logical_name") != "news_items":
            raise NewsReportError(f"not a news manifest: {manifest_path}")
        path = Path(manifest_path).parent / table["file"]
        if hashlib.sha256(path.read_bytes()).hexdigest() != table.get("sha256"):
            raise NewsReportError(f"news hash mismatch: {path}")
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                item = json.loads(line)
                existing = by_id.get(item["news_id"])
                if existing is None or item["fetched_at"] > existing["fetched_at"]:
                    by_id[item["news_id"]] = item
    return sorted(
        by_id.values(),
        key=lambda item: (item["published_at"], item["news_id"]),
        reverse=True,
    )


def render_report(items: Sequence[Dict[str, Any]], report_date: str) -> str:
    counts = Counter(item["event_type"] for item in items)
    lines = [
        f"# {report_date} 财经新闻日报",
        "",
        "本报告是新闻事实索引，不构成事实确认、利好/利空判断或投资建议。重要内容应回到原始来源和公司公告交叉验证。",
        "",
        "## 摘要",
        "",
        f"- 新闻数量：{len(items)}",
    ]
    for event_type, count in sorted(counts.items()):
        lines.append(f"- {event_type}：{count}")
    lines.extend(["", "## 新闻明细", ""])
    if not items:
        lines.append("当日输入批次没有新闻。")
    for item in items:
        name = item.get("security_name") or item.get("source_security_code") or "未关联证券"
        publisher = item.get("publisher") or "来源媒体未标明"
        lines.extend(
            [
                f"### {name}｜{item['event_type']}",
                "",
                f"- 标题：[{item['title']}]({item['url']})",
                f"- 媒体：{publisher}",
                f"- 发布时间：{item['published_at']}",
                "- 数据来源：同花顺问财",
                f"- 来源记录：`{item['raw_record_id']}`",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Generate a factual daily news report.")
    parser.add_argument("manifests", nargs="+", type=Path)
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        content = render_report(load_news(args.manifests), args.date)
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
