import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.export_public_site import (  # noqa: E402
    ExportError,
    MAX_QUEUE_SHARD_BYTES,
    SHARD_NAMES,
    build_public_payloads,
    export_public_site,
)
from scripts.public_payload_safety import PublicPayloadSafetyError  # noqa: E402


def _json_content(value):
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_json_content(value))


def _write_jsonl(path: Path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = b"".join(_json_content(record) for record in records)
    path.write_bytes(content)
    return content


def _public_output(root: Path) -> Path:
    return root / "site/public/data"


def _write_universe_config(root: Path, minimum_expected_count: int = 2):
    _write_json(
        root / "config/investment_universe.json",
        {
            "schema_version": 1,
            "universe_version": "test",
            "stocks": {
                "universe_id": "test-stock-universe",
                "allowed_exchanges": ["SH", "SZ"],
                "excluded_code_prefixes": ["300", "301", "688"],
                "minimum_expected_count": minimum_expected_count,
            },
            "etfs": {
                "universe_id": "test-etfs",
                "allowed_exchanges": ["SH", "SZ"],
            },
        },
    )


def _write_screening(root: Path, date: str, bundle: str, version: str, records):
    directory = root / "data/derived/runs/screening" / Path(date.replace("-", "/")) / bundle
    content = _write_jsonl(directory / "market_research_queue.jsonl", records)
    manifest = {
        "bundle_schema_version": 1,
        "bundle_id": bundle,
        "screener_version": version,
        "screening_version": version,
        "universe_version": "test",
        "universe_id": "test-stock-universe",
        "as_of_date": date,
        "investment_universe": "config/investment_universe.json",
        "purpose": "research_priority_only",
        "coverage": {
            "universe_count": len(records),
            "configured_stock_universe_id": "test-stock-universe",
            "eligible_count": sum(record.get("eligible") is True for record in records),
            "reject_count": sum(record.get("eligible") is False for record in records),
        },
        "table": {
            "logical_name": "market_research_queue",
            "file": "market_research_queue.jsonl",
            "primary_key": ["security_code", "as_of_date", "screening_version"],
            "record_count": len(records),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    _write_json(directory / "manifest.json", manifest)
    return directory / "manifest.json"


def _rewrite_screening_records(manifest_path: Path, records):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    table_path = manifest_path.parent / manifest["table"]["file"]
    content = _write_jsonl(table_path, records)
    manifest["table"]["record_count"] = len(records)
    manifest["table"]["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["coverage"]["universe_count"] = len(records)
    _write_json(manifest_path, manifest)
    return manifest


def _write_single_table_bundle(
    root: Path,
    *,
    date: str,
    bundle: str,
    logical_name: str,
    records,
    fetched_at: str,
):
    directory = root / "data/normalized/runs/iwencai" / Path(date.replace("-", "/")) / bundle
    file_name = f"{logical_name}.jsonl"
    content = _write_jsonl(directory / file_name, records)
    manifest = {
        "bundle_schema_version": 1,
        "bundle_id": bundle,
        "source": "iwencai",
        "fetched_at": fetched_at,
        "coverage": {"record_count": len(records)},
        "table": {
            "logical_name": logical_name,
            "file": file_name,
            "record_count": len(records),
            "sha256": hashlib.sha256(content).hexdigest(),
        },
    }
    manifest_path = directory / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _rewrite_single_table_records(manifest_path: Path, records):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    table = manifest["table"]
    table_path = manifest_path.parent / table["file"]
    content = _write_jsonl(table_path, records)
    table["record_count"] = len(records)
    table["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["coverage"]["record_count"] = len(records)
    _write_json(manifest_path, manifest)
    return manifest


def _refresh_report_source_hashes(root: Path, source_manifest: Path):
    relative = source_manifest.relative_to(root).as_posix()
    digest = hashlib.sha256(source_manifest.read_bytes()).hexdigest()
    for report_manifest_path in (root / "reports/daily/monitoring").glob("**/manifest.json"):
        report_manifest = json.loads(report_manifest_path.read_text(encoding="utf-8"))
        changed = False
        for source in report_manifest.get("source_manifests", []):
            if source.get("path") == relative:
                source["sha256"] = digest
                changed = True
        if changed:
            _write_json(report_manifest_path, report_manifest)


def _write_financial_bundle(root: Path, date: str, bundle: str):
    directory = root / "data/normalized/runs/iwencai" / Path(date.replace("-", "/")) / bundle
    reports = [
        {
            "security_code": "000001.SZ",
            "security_name": "甲公司",
            "period_end": "2025-12-31",
            "report_type": "2025FY",
            "available_from": "2026-01-03",
            "fact_count": 1,
            "raw_snapshot": "data/raw/never-export-this.json",
        },
        {
            "security_code": "300750.SZ",
            "security_name": "证券池外公司",
            "period_end": "2025-12-31",
            "report_type": "2025FY",
            "available_from": "2026-01-03",
            "fact_count": 1,
        },
    ]
    facts = [
        {
            "security_code": "000001.SZ",
            "security_name": "甲公司",
            "period_end": "2025-12-31",
            "report_type": "2025FY",
            "canonical_field_name": "revenue",
            "statement_type": "income_statement",
            "value": 123.0,
            "unit": "CNY",
            "value_status": "present",
            "raw_snapshot": "data/raw/never-export-this.json",
        },
        {
            "security_code": "300750.SZ",
            "security_name": "证券池外公司",
            "period_end": "2025-12-31",
            "report_type": "2025FY",
            "canonical_field_name": "revenue",
            "statement_type": "income_statement",
            "value": 999.0,
            "unit": "CNY",
            "value_status": "present",
        },
    ]
    report_content = _write_jsonl(directory / "financial_reports.jsonl", reports)
    fact_content = _write_jsonl(directory / "financial_facts.jsonl", facts)
    manifest = {
        "bundle_schema_version": 1,
        "bundle_id": bundle,
        "source": "iwencai",
        "fetched_at_start": f"{date}T08:00:00+08:00",
        "fetched_at_end": f"{date}T08:01:00+08:00",
        "tables": {
            "financial_reports": {
                "file": "financial_reports.jsonl",
                "record_count": len(reports),
                "sha256": hashlib.sha256(report_content).hexdigest(),
            },
            "financial_facts": {
                "file": "financial_facts.jsonl",
                "record_count": len(facts),
                "sha256": hashlib.sha256(fact_content).hexdigest(),
            },
        },
    }
    _write_json(directory / "manifest.json", manifest)
    return directory / "manifest.json"


def _write_etf_bundle(root: Path, date: str, bundle: str, records):
    directory = root / "data/normalized/runs/iwencai" / Path(date.replace("-", "/")) / bundle
    content = _write_jsonl(directory / "etf_snapshots.jsonl", records)
    manifest = {
        "bundle_schema_version": 1,
        "bundle_id": bundle,
        "source": "iwencai",
        "fetched_at_start": f"{date}T11:00:00+08:00",
        "fetched_at_end": f"{date}T11:01:00+08:00",
        "universe_id": "test-etfs",
        "universe_version": "test",
        "as_of_date": date,
        "coverage": {"etf_count": len(records)},
        "tables": {
            "etf_snapshots": {
                "file": "etf_snapshots.jsonl",
                "record_count": len(records),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }
    manifest_path = directory / "manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _rewrite_etf_records(manifest_path: Path, records):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    table = manifest["tables"]["etf_snapshots"]
    table_path = manifest_path.parent / table["file"]
    content = _write_jsonl(table_path, records)
    table["record_count"] = len(records)
    table["sha256"] = hashlib.sha256(content).hexdigest()
    manifest["coverage"]["etf_count"] = len(records)
    _write_json(manifest_path, manifest)
    return manifest


def _write_pipeline(root: Path, date: str, run_id: str, finished_at: str):
    path = root / "reports/daily/pipeline-runs" / date / f"{run_id}.json"
    _write_json(
        path,
        {
            "run_schema_version": 1,
            "run_id": run_id,
            "pipeline_version": "1.2.0",
            "status": "succeeded",
            "started_at": finished_at.replace(":59", ":00"),
            "finished_at": finished_at,
            "step_count": 1,
            "steps": [
                {
                    "stage": "validation",
                    "step_id": "repository_integrity",
                    "status": "succeeded",
                    "exit_code": 0,
                    "error_type": None,
                    "started_at": finished_at.replace(":59", ":00"),
                    "finished_at": finished_at,
                    "command": ["secret-command-must-not-be-public"],
                }
            ],
            "readiness": {
                "status": "up_to_date",
                "planned_step_count": 0,
                "incomplete_job_count": 0,
                "research": {
                    "status": "up_to_date",
                    "screening": {"status": "up_to_date"},
                    "monitoring": {"matched_snapshot_count": 2},
                    "raw_snapshot": "data/raw/never-export-this.json",
                },
            },
        },
    )
    return path


def _write_monitoring_report(
    root: Path,
    *,
    kind: str,
    date: str,
    bundle: str,
    source_manifest: Path,
    record_count: int,
):
    directory = root / "reports/daily/monitoring" / kind / Path(date.replace("-", "/")) / bundle
    report_name = "financial-news.md" if kind == "news" else "announcement-events.md"
    report_content = f"# {kind} report {date}\n\n公开事实索引。\n".encode("utf-8")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / report_name).write_bytes(report_content)
    source_content = source_manifest.read_bytes()
    _write_json(
        directory / "manifest.json",
        {
            "monitoring_report_schema_version": 1,
            "bundle_id": bundle,
            "kind": kind,
            "as_of_date": date,
            "source_manifests": [
                {
                    "path": source_manifest.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(source_content).hexdigest(),
                }
            ],
            "coverage": {"record_count": record_count},
            "report": {
                "file": report_name,
                "sha256": hashlib.sha256(report_content).hexdigest(),
            },
        },
    )


def _build_fixture(root: Path):
    _write_universe_config(root)
    old_records = [
        {
            "as_of_date": "2026-01-01",
            "security_code": "000001.SZ",
            "security_name": "旧公司",
            "eligible": True,
            "priority": "P0",
            "rank": 1,
        }
    ]
    latest_records = [
        {
            "as_of_date": "2026-01-02",
            "security_code": "000001.SZ",
            "security_name": "甲公司",
            "eligible": True,
            "priority": "P0",
            "rank": 1,
            "score": 0.9,
            "market_cap": 100.0,
            "raw_snapshot": "data/raw/never-export-this.json",
        },
        {
            "as_of_date": "2026-01-02",
            "security_code": "600001.SH",
            "security_name": "乙公司",
            "eligible": False,
            "priority": "Reject",
            "rank": 2,
            "eligibility_reasons": ["missing_metric"],
        },
    ]
    _write_screening(root, "2026-01-01", "screen-old", "1.0.0", old_records)
    latest_screen = _write_screening(
        root, "2026-01-02", "screen-latest", "1.1.0", latest_records
    )
    latest_financial = _write_financial_bundle(root, "2026-01-03", "financial-latest")
    _write_etf_bundle(
        root,
        "2026-01-03",
        "etf-old",
        [
            {
                "universe_id": "test-etfs",
                "universe_version": "test",
                "etf_code": "513100.SH",
                "etf_name": "旧ETF",
                "exchange": "SH",
                "as_of_date": "2026-01-03",
                "tracked_index": "纳斯达克100指数",
                "fund_type": "ETF",
            }
        ],
    )
    latest_etf = _write_etf_bundle(
        root,
        "2026-01-06",
        "etf-latest",
        [
            {
                "universe_id": "test-etfs",
                "universe_version": "test",
                "etf_code": "513100.SH",
                "etf_name": "纳指ETF",
                "exchange": "SH",
                "as_of_date": "2026-01-06",
                "tracked_index": "纳斯达克100指数",
                "tracked_index_family": "nasdaq_100",
                "fund_type": "ETF",
                "fund_type_memberships": ["ETF", "跨境ETF"],
                "listing_date": "2013-05-15",
                "listing_status": "正常上市",
                "price": 1.5,
                "fund_size": 1000.0,
                "premium_discount_rate": 0.67,
                "raw_snapshot": "data/raw/never-export-this.json",
            }
        ],
    )

    old_news = _write_single_table_bundle(
        root,
        date="2026-01-03",
        bundle="news-old",
        logical_name="news_items",
        records=[{"news_id": "old", "title": "旧闻"}],
        fetched_at="2026-01-03T10:00:00+08:00",
    )
    self_news = _write_single_table_bundle(
        root,
        date="2026-01-04",
        bundle="news-latest-a",
        logical_name="news_items",
        records=[
            {
                "news_id": "news-a",
                "published_at": "2026-01-04T09:00:00+08:00",
                "security_code": "000001.SZ",
                "security_name": "甲公司",
                "title": "公司新闻",
                "summary": "公开摘要",
                "url": "https://example.com/news-a",
                "raw_item": {"token": "never-export-this"},
            }
        ],
        fetched_at="2026-01-04T10:00:00+08:00",
    )
    second_news = _write_single_table_bundle(
        root,
        date="2026-01-04",
        bundle="news-latest-b",
        logical_name="news_items",
        records=[
            {
                "news_id": "news-b",
                "published_at": "2026-01-04T10:00:00+08:00",
                "title": "宏观新闻",
                "summary": "第二条公开摘要",
            }
        ],
        fetched_at="2026-01-04T10:01:00+08:00",
    )
    event_manifest = _write_single_table_bundle(
        root,
        date="2026-01-04",
        bundle="events-latest",
        logical_name="events",
        records=[],
        fetched_at="2026-01-04T10:02:00+08:00",
    )
    _write_monitoring_report(
        root,
        kind="news",
        date="2026-01-04",
        bundle="news-report-a",
        source_manifest=self_news,
        record_count=1,
    )
    _write_monitoring_report(
        root,
        kind="news",
        date="2026-01-04",
        bundle="news-report-b",
        source_manifest=second_news,
        record_count=1,
    )
    _write_monitoring_report(
        root,
        kind="events",
        date="2026-01-04",
        bundle="events-report",
        source_manifest=event_manifest,
        record_count=0,
    )
    daily = root / "reports/daily/2026-01-04-research-validation.md"
    daily.parent.mkdir(parents=True, exist_ok=True)
    daily.write_text("# 研究数据验证\n", encoding="utf-8")
    _write_pipeline(root, "2026-01-04", "run-old", "2026-01-04T12:00:59+00:00")
    latest_pipeline = _write_pipeline(
        root, "2026-01-05", "run-latest", "2026-01-05T12:00:59+00:00"
    )
    return {
        "latest_screen": latest_screen,
        "latest_etf": latest_etf,
        "latest_financial": latest_financial,
        "latest_news": self_news,
        "latest_pipeline": latest_pipeline,
        "old_news": old_news,
    }


class ExportPublicSiteTests(unittest.TestCase):
    def test_export_is_deterministic_and_selects_latest_source_batches(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            _build_fixture(root)
            output = _public_output(root)
            export_public_site(root, output)

            first_files = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*.json")
            }
            export_public_site(root, output)
            second_files = {
                path.relative_to(output).as_posix(): path.read_bytes()
                for path in output.rglob("*.json")
            }
            self.assertEqual(first_files, second_files)

            index = json.loads(first_files["index.json"])
            self.assertEqual(index["generatedAt"], "2026-01-06T11:01:00+08:00")
            self.assertEqual(index["fileCount"], 38)
            for descriptor in index["files"]:
                content_bytes = first_files[descriptor["path"]]
                self.assertEqual(descriptor["bytes"], len(content_bytes))
                self.assertEqual(
                    descriptor["sha256"], hashlib.sha256(content_bytes).hexdigest()
                )
            market = json.loads(first_files["market/summary.json"])
            self.assertEqual(market["bundleId"], "screen-latest")
            self.assertEqual(market["recordCount"], 2)
            status = json.loads(first_files["status/latest.json"])
            self.assertTrue(status["artifactAvailable"])
            self.assertEqual(status["status"], "ready")
            self.assertEqual(status["run"]["runId"], "run-latest")
            self.assertNotIn("secret-command", first_files["status/latest.json"].decode())

            content = json.loads(first_files["content/index.json"])
            self.assertEqual({item["newsId"] for item in content["news"]}, {"news-a", "news-b"})
            self.assertEqual(content["domains"]["events"]["status"], "empty")
            self.assertEqual(len(content["reports"]), 4)
            etf = json.loads(first_files["etf/index.json"])
            self.assertEqual(etf["status"], "ready")
            self.assertEqual(etf["recordCount"], 1)
            self.assertEqual(etf["records"][0]["etfCode"], "513100.SH")
            self.assertEqual(etf["records"][0]["fundSize"], 1000.0)
            self.assertNotIn("rawSnapshot", etf["records"][0])
            companies = json.loads(first_files["companies/index.json"])
            self.assertEqual(companies["recordCount"], market["recordCount"])
            self.assertEqual(
                {item["securityCode"] for item in companies["companies"]},
                {"000001.SZ", "600001.SH"},
            )
            self.assertEqual(index["datasets"]["financial"]["recordCount"], 2)
            detail_text = b"".join(
                first_files[f"companies/details-{shard}.json"]
                for shard in SHARD_NAMES
            ).decode("utf-8")
            self.assertNotIn("300750.SZ", detail_text)
            provenance = json.loads(first_files["provenance/index.json"])
            self.assertIn("etf", {source["domain"] for source in provenance["sources"]})
            joined = b"".join(first_files.values()).decode("utf-8").casefold()
            self.assertNotIn("data/raw/", joined)
            self.assertNotIn("never-export-this", joined)

    def test_hash_and_record_count_mismatches_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_screen"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            queue = manifest_path.parent / manifest["table"]["file"]

            original = queue.read_bytes()
            queue.write_bytes(original + b"\n")
            with self.assertRaisesRegex(ExportError, "sha256 mismatch"):
                export_public_site(root, _public_output(root))

            queue.write_bytes(original)
            manifest["table"]["record_count"] += 1
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ExportError, "record_count mismatch"):
                export_public_site(root, _public_output(root))

    def test_etf_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_etf"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            table = manifest["tables"]["etf_snapshots"]
            table_path = manifest_path.parent / table["file"]
            original = table_path.read_bytes()
            table_path.write_bytes(b'{"tampered":true}\n')
            with self.assertRaisesRegex(ExportError, "sha256 mismatch"):
                export_public_site(root, _public_output(root))
            table_path.write_bytes(original)
            table["record_count"] += 1
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ExportError, "record_count mismatch"):
                export_public_site(root, _public_output(root))

    def test_etf_identity_exchange_date_and_uniqueness_are_verified(self):
        manifest_mutations = (
            ("universe_id", "other-etfs", "manifest universe_id"),
            ("universe_version", "other-version", "manifest universe_version"),
            ("as_of_date", "2026-01-05", "as_of_date|path date"),
        )
        for field, value, expected in manifest_mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_etf"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = value
                _write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

        record_mutations = (
            ("universe_id", "other-etfs", "record universe_id"),
            ("universe_version", "other-version", "record universe_version"),
            ("exchange", "SZ", "code/exchange"),
            ("as_of_date", "2026-01-05", "record as_of_date mismatch"),
            ("as_of_date", "2026-1-6", "invalid as_of_date"),
        )
        for field, value, expected in record_mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_etf"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                table_path = manifest_path.parent / manifest["tables"]["etf_snapshots"]["file"]
                records = [
                    json.loads(line)
                    for line in table_path.read_text(encoding="utf-8").splitlines()
                ]
                records[0][field] = value
                _rewrite_etf_records(manifest_path, records)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_etf"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            table_path = manifest_path.parent / manifest["tables"]["etf_snapshots"]["file"]
            records = [
                json.loads(line)
                for line in table_path.read_text(encoding="utf-8").splitlines()
            ]
            records.append(dict(records[0]))
            _rewrite_etf_records(manifest_path, records)
            with self.assertRaisesRegex(ExportError, "duplicate code/date"):
                export_public_site(root, _public_output(root))

    def test_screening_below_configured_minimum_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            _build_fixture(root)
            _write_universe_config(root, minimum_expected_count=3)
            with self.assertRaisesRegex(ExportError, "minimum_expected_count"):
                export_public_site(root, _public_output(root))

    def test_screening_rejects_invalid_and_duplicate_security_codes(self):
        for case in ("invalid", "duplicate", "excluded"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_screen"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                table_path = manifest_path.parent / manifest["table"]["file"]
                records = [
                    json.loads(line)
                    for line in table_path.read_text(encoding="utf-8").splitlines()
                ]
                if case == "invalid":
                    records[0]["security_code"] = "000001"
                    expected = "invalid security_code"
                elif case == "excluded":
                    records[0]["security_code"] = "300001.SZ"
                    expected = "outside the configured universe"
                else:
                    records.append(dict(records[0]))
                    expected = "duplicate security_code"
                _rewrite_screening_records(manifest_path, records)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

    def test_screening_manifest_must_match_configured_universe(self):
        mutations = (
            ("investment_universe", "config/other.json", "investment_universe"),
            ("universe_version", "other-version", "universe_version"),
            ("universe_id", "other-universe", "universe_id"),
            ("coverage", "other-universe", "configured universe_id"),
        )
        for field, value, expected in mutations:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_screen"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if field == "coverage":
                    manifest["coverage"]["configured_stock_universe_id"] = value
                else:
                    manifest[field] = value
                _write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

    def test_screening_rows_are_bound_to_source_as_of_date(self):
        cases = (
            (None, "missing or invalid as_of_date"),
            ("2026-1-2", "missing or invalid as_of_date"),
            ("2026-01-01", "table as_of_date mismatch"),
        )
        for value, expected in cases:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_screen"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                table_path = manifest_path.parent / manifest["table"]["file"]
                records = [
                    json.loads(line)
                    for line in table_path.read_text(encoding="utf-8").splitlines()
                ]
                if value is None:
                    records[0].pop("as_of_date")
                else:
                    records[0]["as_of_date"] = value
                _rewrite_screening_records(manifest_path, records)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_screen"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["as_of_date"] = "2026-01-03"
            _write_json(manifest_path, manifest)
            with self.assertRaisesRegex(ExportError, "as_of_date|path date"):
                export_public_site(root, _public_output(root))

    def test_selected_json_and_jsonl_are_strict(self):
        for case, expected in (
            ("duplicate_manifest_key", "duplicate object keys"),
            ("duplicate_row_key", "duplicate object keys"),
            ("non_finite_row", "non-finite number"),
        ):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_screen"]
                if case == "duplicate_manifest_key":
                    text = manifest_path.read_text(encoding="utf-8")
                    marker = '"as_of_date":"2026-01-02",'
                    manifest_path.write_text(
                        text.replace(marker, marker + marker, 1), encoding="utf-8"
                    )
                else:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    table_path = manifest_path.parent / manifest["table"]["file"]
                    lines = table_path.read_text(encoding="utf-8").splitlines()
                    if case == "duplicate_row_key":
                        lines[0] = lines[0].replace('"rank":1,', '"rank":1,"rank":1,', 1)
                    else:
                        lines[0] = lines[0].replace('"score":0.9', '"score":NaN', 1)
                    content = ("\n".join(lines) + "\n").encode("utf-8")
                    table_path.write_bytes(content)
                    manifest["table"]["sha256"] = hashlib.sha256(content).hexdigest()
                    _write_json(manifest_path, manifest)
                with self.assertRaisesRegex(ExportError, expected):
                    export_public_site(root, _public_output(root))

    def test_content_requires_stable_nonempty_identifiers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_news"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            table_path = manifest_path.parent / manifest["table"]["file"]
            records = [json.loads(line) for line in table_path.read_text().splitlines()]
            records[0].pop("news_id")
            _rewrite_single_table_records(manifest_path, records)
            _refresh_report_source_hashes(root, manifest_path)
            with self.assertRaisesRegex(ExportError, "missing identifier"):
                export_public_site(root, _public_output(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            second_manifest = root / "data/normalized/runs/iwencai/2026/01/04/news-latest-b/manifest.json"
            manifest = json.loads(second_manifest.read_text(encoding="utf-8"))
            table_path = second_manifest.parent / manifest["table"]["file"]
            records = [json.loads(line) for line in table_path.read_text().splitlines()]
            records[0]["news_id"] = "news-a"
            _rewrite_single_table_records(second_manifest, records)
            _refresh_report_source_hashes(root, second_manifest)
            with self.assertRaisesRegex(ExportError, "conflicting duplicate identifiers"):
                export_public_site(root, _public_output(root))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            first_manifest = paths["latest_news"]
            first_descriptor = json.loads(first_manifest.read_text(encoding="utf-8"))["table"]
            first_path = first_manifest.parent / first_descriptor["file"]
            duplicate = json.loads(first_path.read_text(encoding="utf-8").splitlines()[0])
            second_manifest = root / "data/normalized/runs/iwencai/2026/01/04/news-latest-b/manifest.json"
            _rewrite_single_table_records(second_manifest, [duplicate])
            _refresh_report_source_hashes(root, second_manifest)
            output = _public_output(root)
            export_public_site(root, output)
            content = json.loads((output / "content/index.json").read_text(encoding="utf-8"))
            self.assertEqual([item["newsId"] for item in content["news"]], ["news-a"])

    def test_sensitive_allowed_field_value_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_screen"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            queue = manifest_path.parent / manifest["table"]["file"]
            records = [json.loads(line) for line in queue.read_text(encoding="utf-8").splitlines()]
            records[0]["security_name"] = "Bearer test_only_opaque_token_123456789"
            content = _write_jsonl(queue, records)
            manifest["table"]["sha256"] = hashlib.sha256(content).hexdigest()
            _write_json(manifest_path, manifest)
            with self.assertRaises(PublicPayloadSafetyError):
                export_public_site(root, _public_output(root))

    def test_pages_payload_rejects_private_repository_paths_in_public_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            manifest_path = paths["latest_screen"]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            table_path = manifest_path.parent / manifest["table"]["file"]
            records = [
                json.loads(line)
                for line in table_path.read_text(encoding="utf-8").splitlines()
            ]
            records[0]["security_name"] = "data/raw/never-publish.json"
            _rewrite_screening_records(manifest_path, records)
            with self.assertRaisesRegex(ExportError, "private repository path"):
                export_public_site(root, _public_output(root))

    def test_content_urls_allow_only_safe_absolute_http_and_https(self):
        cases = (
            "javascript:alert(1)",
            "data:text/html,unsafe",
            "file:///tmp/unsafe",
            "https://test-user:test-password@example.invalid/news",
            "https://example.invalid/news?access%5Ftoken=test-only-secret",
            "https://example.invalid/news?X-Amz-Signature=test-only-secret",
            "https://example.com/news#access_token=test-only-secret",
            "http://localhost/news",
            "http://service.local/news",
            "http://127.0.0.1/news",
            "http://10.0.0.1/news",
            "http://169.254.1.1/news",
            "http://0.0.0.0/news",
            "http://[::1]/news",
            "http://[fc00::1]/news",
            "http://[fe80::1]/news",
            "http://[::]/news",
        )
        for url in cases:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_news"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                table = manifest["table"]
                table_path = manifest_path.parent / table["file"]
                records = [
                    json.loads(line)
                    for line in table_path.read_text(encoding="utf-8").splitlines()
                ]
                records[0]["url"] = url
                content = _write_jsonl(table_path, records)
                table["sha256"] = hashlib.sha256(content).hexdigest()
                _write_json(manifest_path, manifest)
                with self.assertRaises((ExportError, PublicPayloadSafetyError)):
                    export_public_site(root, _public_output(root))

    def test_content_urls_allow_public_remote_paths_that_look_local(self):
        for url in (
            "https://finance.example.com/home/article?symbol=000001#research",
            "https://finance.example.com/tmp/market-note",
            "https://finance.example.com/Volumes/public-report",
            "https://finance.example.com/data/raw/public-article",
        ):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "repository"
                paths = _build_fixture(root)
                manifest_path = paths["latest_news"]
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                table_path = manifest_path.parent / manifest["table"]["file"]
                records = [
                    json.loads(line)
                    for line in table_path.read_text(encoding="utf-8").splitlines()
                ]
                records[0]["url"] = url
                _rewrite_single_table_records(manifest_path, records)
                _refresh_report_source_hashes(root, manifest_path)
                output = _public_output(root)
                export_public_site(root, output)
                content = json.loads((output / "content/index.json").read_text())
                self.assertEqual(content["news"][1]["url"], url)

    def test_stable_sha256_shards_obey_publication_limit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            _build_fixture(root)
            output = _public_output(root)
            export_public_site(root, output)
            for shard in SHARD_NAMES:
                path = output / f"market/queue-{shard}.json"
                self.assertLess(len(path.read_bytes()), MAX_QUEUE_SHARD_BYTES)
                payload = json.loads(path.read_text(encoding="utf-8"))
                for record in payload["records"]:
                    expected = hashlib.sha256(record["securityCode"].encode("utf-8")).hexdigest()[0]
                    self.assertEqual(expected, shard)

            manifest_path = root / "data/derived/runs/screening/2026/01/02/screen-latest/manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            queue = manifest_path.parent / manifest["table"]["file"]
            huge = [
                {
                    "as_of_date": "2026-01-02",
                    "security_code": "000001.SZ",
                    "security_name": "甲" * MAX_QUEUE_SHARD_BYTES,
                    "eligible": True,
                    "priority": "P0",
                    "rank": 1,
                }
            ]
            content = _write_jsonl(queue, huge)
            manifest["coverage"]["universe_count"] = 1
            manifest["table"]["record_count"] = 1
            manifest["table"]["sha256"] = hashlib.sha256(content).hexdigest()
            _write_json(manifest_path, manifest)
            _write_universe_config(root, minimum_expected_count=1)
            with self.assertRaisesRegex(ExportError, "250 KiB"):
                export_public_site(root, _public_output(root))

    def test_missing_domains_are_explicit_and_still_emit_all_shards(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "empty-repository"
            root.mkdir()
            output = _public_output(root)
            export_public_site(root, output)
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertIsNone(index["generatedAt"])
            self.assertEqual(index["status"], "partial")
            self.assertTrue(
                all(index["datasets"][domain]["status"] == "missing" for domain in index["datasets"])
            )
            pipeline = json.loads(
                (output / "status/latest.json").read_text(encoding="utf-8")
            )
            self.assertFalse(pipeline["artifactAvailable"])
            etf = json.loads((output / "etf/index.json").read_text(encoding="utf-8"))
            self.assertEqual(etf, {
                "schemaVersion": 1,
                "status": "missing",
                "asOfDate": None,
                "recordCount": 0,
                "records": [],
            })
            for shard in SHARD_NAMES:
                payload = json.loads((output / f"market/queue-{shard}.json").read_text())
                self.assertEqual(payload["status"], "missing")
                self.assertEqual(payload["records"], [])

    def test_pipeline_artifact_availability_does_not_imply_readiness(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            paths = _build_fixture(root)
            pipeline_path = paths["latest_pipeline"]
            run = json.loads(pipeline_path.read_text(encoding="utf-8"))
            run["readiness"]["status"] = "waiting_for_complete_input"
            run["readiness"]["incomplete_job_count"] = 5
            run["readiness"]["research"]["status"] = "waiting_for_inputs"
            _write_json(pipeline_path, run)

            output = _public_output(root)
            export_public_site(root, output)
            status = json.loads((output / "status/latest.json").read_text(encoding="utf-8"))
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertTrue(status["artifactAvailable"])
            self.assertEqual(status["status"], "partial")
            self.assertEqual(index["datasets"]["pipeline"]["status"], "partial")
            self.assertEqual(index["status"], "partial")

            run["status"] = "failed"
            _write_json(pipeline_path, run)
            export_public_site(root, output)
            status = json.loads((output / "status/latest.json").read_text(encoding="utf-8"))
            index = json.loads((output / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(status["status"], "failed")
            self.assertEqual(index["status"], "failed")

    def test_output_is_restricted_to_fixed_non_symlink_site_data_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repository"
            root.mkdir()
            outside = Path(temporary) / "outside"
            rejected = (
                outside,
                Path("site/public/../../scripts"),
                root / "scripts",
                root,
                root.parent,
            )
            for output in rejected:
                with self.subTest(output=str(output)):
                    with self.assertRaises(ExportError):
                        export_public_site(root, output)

            target = root / "symlink-target"
            target.mkdir()
            marker = target / "must-survive.txt"
            marker.write_text("keep", encoding="utf-8")
            (root / "site").mkdir()
            try:
                os.symlink(target, root / "site/public")
            except (NotImplementedError, OSError):
                self.skipTest("symbolic links are unavailable")
            with self.assertRaisesRegex(ExportError, "symbolic links"):
                export_public_site(root, _public_output(root))
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_real_repository_discovery_smoke_is_partial(self):
        payloads, metadata = build_public_payloads(REPOSITORY_ROOT)
        self.assertGreater(metadata["datasets"]["market"]["recordCount"], 0)
        self.assertEqual(
            payloads["companies/index.json"]["recordCount"],
            metadata["datasets"]["market"]["recordCount"],
        )
        self.assertNotIn(
            "300750.SZ",
            {item["securityCode"] for item in payloads["companies/index.json"]["companies"]},
        )
        self.assertTrue(
            all(
                company["securityCode"] != "300750.SZ"
                for shard in SHARD_NAMES
                for company in payloads[f"companies/details-{shard}.json"]["companies"]
            )
        )
        self.assertEqual(metadata["datasets"]["pipeline"]["status"], "partial")
        self.assertEqual(metadata["status"], "partial")
        self.assertTrue(payloads["status/latest.json"]["artifactAvailable"])
        self.assertEqual(payloads["status/latest.json"]["status"], "partial")
        self.assertEqual(payloads["etf/index.json"]["status"], "missing")


if __name__ == "__main__":
    unittest.main()
