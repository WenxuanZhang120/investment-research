import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_market import (  # noqa: E402
    NormalizationError,
    build_normalized_tables,
    normalize_snapshot,
)


REAL_SNAPSHOT = REPOSITORY_ROOT / (
    "data/raw/iwencai/2026/08/08/"
    "20260808T171511408147+0800_004c42e90d64b30c62fc.json"
)


def canonical_payload(payload):
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class NormalizeIwencaiMarketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.snapshot = self.root / "snapshot.json"
        self.payload = {
            "data": {
                "answer": [
                    {
                        "txt": [
                            {
                                "content": {
                                    "components": [
                                        {
                                            "data": {
                                                "columns": [
                                                    {
                                                        "key": "股票代码",
                                                        "index_name": "股票代码",
                                                        "source": "fixed_index",
                                                        "type": "STR",
                                                        "unit": "",
                                                    },
                                                    {
                                                        "key": "股票简称",
                                                        "index_name": "股票简称",
                                                        "source": "fixed_index",
                                                        "type": "STR",
                                                        "unit": "",
                                                    },
                                                    {
                                                        "key": "收盘价:不复权[20260807]",
                                                        "index_name": "收盘价:不复权",
                                                        "source": "new_parser",
                                                        "type": "DOUBLE",
                                                        "unit": "元",
                                                        "timestamp": "20260807",
                                                    },
                                                    {
                                                        "key": "总市值[20260807]",
                                                        "index_name": "总市值",
                                                        "source": "new_parser",
                                                        "type": "DOUBLE",
                                                        "unit": "元",
                                                        "timestamp": "20260807",
                                                    },
                                                    {
                                                        "key": "市盈率(pe,ttm)[20260807]",
                                                        "index_name": "市盈率(pe,ttm)",
                                                        "source": "new_parser",
                                                        "type": "DOUBLE",
                                                        "unit": "",
                                                        "timestamp": "20260807",
                                                    },
                                                    {
                                                        "key": "股票市场类型",
                                                        "index_name": "股票市场类型",
                                                        "source": "new_parser",
                                                        "type": "STR",
                                                        "unit": "",
                                                    },
                                                    {
                                                        "key": "自动附加字段[20260807]",
                                                        "index_name": "自动附加字段",
                                                        "source": "add_condition",
                                                        "type": "DOUBLE",
                                                        "unit": "%",
                                                    },
                                                ],
                                                "datas": [
                                                    {
                                                        "股票代码": "000001.SZ",
                                                        "股票简称": "平安银行",
                                                        "收盘价:不复权[20260807]": 12.5,
                                                        "总市值[20260807]": 250000000000,
                                                        "市盈率(pe,ttm)[20260807]": 6.2,
                                                        "股票市场类型": "a股",
                                                        "自动附加字段[20260807]": 1,
                                                    },
                                                    {
                                                        "股票代码": "600000.SH",
                                                        "股票简称": "浦发银行",
                                                        "收盘价:不复权[20260807]": "10.25",
                                                        "总市值[20260807]": 300000000000,
                                                        "市盈率(pe,ttm)[20260807]": 5.8,
                                                        "股票市场类型": "a股",
                                                        "自动附加字段[20260807]": 2,
                                                    },
                                                ],
                                            }
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                ]
            }
        }
        self.write_snapshot(self.payload)

    def write_snapshot(self, payload) -> None:
        envelope = {
            "metadata": {
                "source": "iwencai",
                "query": "test query",
                "fetched_at": "2026-08-08T17:15:11.408147+08:00",
                "record_id": "test-record-id",
                "schema_version": 1,
                "payload_sha256": hashlib.sha256(canonical_payload(payload)).hexdigest(),
            },
            "payload": payload,
        }
        self.snapshot.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_builds_three_tables_with_field_lineage(self) -> None:
        built = build_normalized_tables(self.snapshot)

        self.assertEqual(
            {name: len(records) for name, records in built["tables"].items()},
            {
                "security_master": 2,
                "market_bars_daily": 2,
                "valuation_snapshots": 2,
            },
        )
        security = built["tables"]["security_master"][0]
        market_bar = built["tables"]["market_bars_daily"][0]
        valuation = built["tables"]["valuation_snapshots"][0]
        self.assertEqual(security["security_code"], "000001.SZ")
        self.assertEqual(security["observed_date"], "2026-08-08")
        self.assertEqual(market_bar["trade_date"], "2026-08-07")
        self.assertEqual(market_bar["adjustment_type"], "unadjusted")
        self.assertEqual(market_bar["currency"], "CNY")
        self.assertEqual(valuation["as_of_date"], "2026-08-07")
        self.assertEqual(valuation["market_cap_currency"], "CNY")
        self.assertEqual(
            market_bar["field_lineage"]["close"]["raw_field_name"],
            "收盘价:不复权[20260807]",
        )
        self.assertEqual(built["unmapped_fields"], ["自动附加字段[20260807]"])

    def test_writes_atomic_bundle_and_refuses_overwrite(self) -> None:
        normalized_root = self.root / "normalized"
        destination = normalize_snapshot(
            self.snapshot,
            normalized_root=normalized_root,
        )

        self.assertEqual(destination.name, "test-record-id")
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["normalizer_version"], "1.0.0")
        self.assertEqual(manifest["tables"]["security_master"]["record_count"], 2)
        self.assertTrue((destination / "security_master.jsonl").is_file())
        self.assertTrue((destination / "market_bars_daily.jsonl").is_file())
        self.assertTrue((destination / "valuation_snapshots.jsonl").is_file())

        with self.assertRaises(FileExistsError):
            normalize_snapshot(self.snapshot, normalized_root=normalized_root)

    def test_rejects_tampered_raw_payload(self) -> None:
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["payload"]["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]["datas"][0]["股票简称"] = "被修改"
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(NormalizationError, "checksum"):
            build_normalized_tables(self.snapshot)

    def test_observed_date_uses_project_timezone(self) -> None:
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["metadata"]["fetched_at"] = "2026-08-07T17:30:00+00:00"
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        built = build_normalized_tables(self.snapshot)

        self.assertEqual(
            built["tables"]["security_master"][0]["observed_date"],
            "2026-08-08",
        )

    def test_rejects_missing_required_value(self) -> None:
        del self.payload["data"]["answer"][0]["txt"][0]["content"]["components"][0][
            "data"
        ]["datas"][0]["总市值[20260807]"]
        self.write_snapshot(self.payload)

        with self.assertRaisesRegex(NormalizationError, "总市值"):
            build_normalized_tables(self.snapshot)

    def test_real_snapshot_produces_fifty_records_per_table(self) -> None:
        built = build_normalized_tables(REAL_SNAPSHOT)

        self.assertEqual(
            {name: len(records) for name, records in built["tables"].items()},
            {
                "security_master": 50,
                "market_bars_daily": 50,
                "valuation_snapshots": 50,
            },
        )
        self.assertEqual(
            built["tables"]["market_bars_daily"][0]["security_code"],
            "002731.SZ",
        )
        self.assertEqual(len(built["unmapped_fields"]), 13)


if __name__ == "__main__":
    unittest.main()
