import copy
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
    build_normalized_batch,
    build_normalized_tables,
    normalize_snapshot,
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
                                                "meta": {
                                                    "page": 1,
                                                    "limit": 100,
                                                    "code_count": 2,
                                                },
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
        universe = json.loads(
            (REPOSITORY_ROOT / "config/investment_universe.json").read_text(
                encoding="utf-8"
            )
        )
        universe["stocks"]["minimum_expected_count"] = 1
        self.universe_path = self.root / "investment_universe.json"
        self.universe_path.write_text(
            json.dumps(universe, ensure_ascii=False), encoding="utf-8"
        )
        self.write_snapshot(self.payload)

    def write_snapshot(
        self,
        payload,
        *,
        path=None,
        record_id="test-record-id",
        query="test query",
    ) -> Path:
        destination = Path(path) if path is not None else self.snapshot
        destination.parent.mkdir(parents=True, exist_ok=True)
        envelope = {
            "metadata": {
                "source": "iwencai",
                "query": query,
                "fetched_at": "2026-08-08T17:15:11.408147+08:00",
                "as_of_date": "2026-08-07",
                "record_id": record_id,
                "schema_version": 1,
                "payload_sha256": hashlib.sha256(canonical_payload(payload)).hexdigest(),
            },
            "payload": payload,
        }
        destination.write_text(
            json.dumps(envelope, ensure_ascii=False),
            encoding="utf-8",
        )
        return destination

    def synthetic_market_row(self, index):
        return {
            "股票代码": f"{600000 + index:06d}.SH",
            "股票简称": f"测试证券{index:04d}",
            "收盘价:不复权[20260807]": 10 + index / 10000,
            "总市值[20260807]": 1000000000 + index,
            "市盈率(pe,ttm)[20260807]": 10 + index / 100000,
            "股票市场类型": ["全部A股", "沪深主板A股"],
            "自动附加字段[20260807]": index,
        }

    def synthetic_market_page(self, *, start, count, page, limit, total):
        payload = copy.deepcopy(self.payload)
        table = payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        table["datas"] = [
            self.synthetic_market_row(index)
            for index in range(start, start + count)
        ]
        table["meta"] = {
            "page": page,
            "limit": limit,
            "code_count": total,
        }
        return payload

    def set_minimum_expected_count(self, count):
        universe = json.loads(self.universe_path.read_text(encoding="utf-8"))
        universe["stocks"]["minimum_expected_count"] = count
        self.universe_path.write_text(
            json.dumps(universe, ensure_ascii=False), encoding="utf-8"
        )

    def test_builds_three_tables_with_field_lineage(self) -> None:
        built = build_normalized_tables(
            self.snapshot, repository_root=self.root
        )

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
        self.assertEqual(security["exchange"], "SZ")
        self.assertEqual(security["market_memberships"], ["a股"])
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
            universe_path=self.universe_path,
            normalized_root=normalized_root,
            repository_root=self.root,
        )

        self.assertEqual(destination.name, "test-record-id")
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["normalizer_version"], "2.2.0")
        self.assertEqual(manifest["bundle_schema_version"], 2)
        self.assertEqual(manifest["tables"]["security_master"]["record_count"], 2)
        self.assertTrue((destination / "security_master.jsonl").is_file())
        self.assertTrue((destination / "market_bars_daily.jsonl").is_file())
        self.assertTrue((destination / "valuation_snapshots.jsonl").is_file())

        with self.assertRaises(FileExistsError):
            normalize_snapshot(
                self.snapshot,
                universe_path=self.universe_path,
                normalized_root=normalized_root,
                repository_root=self.root,
            )

    def test_rejects_complete_but_below_minimum_market_scope(self) -> None:
        universe = json.loads(self.universe_path.read_text(encoding="utf-8"))
        universe["stocks"]["minimum_expected_count"] = 3
        self.universe_path.write_text(
            json.dumps(universe, ensure_ascii=False), encoding="utf-8"
        )

        with self.assertRaisesRegex(
            NormalizationError,
            "reported total 2 is below configured minimum 3",
        ):
            build_normalized_batch(
                [self.snapshot],
                universe_path=self.universe_path,
                repository_root=self.root,
            )

    def test_rejects_market_batch_without_complete_pagination_metadata(self) -> None:
        table = self.payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        del table["meta"]["code_count"]
        self.write_snapshot(self.payload)

        with self.assertRaisesRegex(
            NormalizationError,
            "requires page, limit, and reported total metadata",
        ):
            build_normalized_batch(
                [self.snapshot],
                universe_path=self.universe_path,
                repository_root=self.root,
            )

    def test_rejects_tampered_raw_payload(self) -> None:
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["payload"]["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]["datas"][0]["股票简称"] = "被修改"
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(NormalizationError, "checksum"):
            build_normalized_tables(
                self.snapshot, repository_root=self.root
            )

    def test_observed_date_uses_project_timezone(self) -> None:
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["metadata"]["fetched_at"] = "2026-08-07T17:30:00+00:00"
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        built = build_normalized_tables(
            self.snapshot, repository_root=self.root
        )

        self.assertEqual(
            built["tables"]["security_master"][0]["observed_date"],
            "2026-08-08",
        )

    def test_accepts_observed_market_aliases_and_membership_lists(self) -> None:
        table = self.payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        replacements = {
            "收盘价:不复权[20260807]": "收盘价_不复权[20260807]",
            "市盈率(pe,ttm)[20260807]": "最新市盈率ttm",
        }
        for column in table["columns"]:
            old_name = column.get("key")
            if old_name in replacements:
                new_name = replacements[old_name]
                column["key"] = new_name
                column["index_name"] = new_name.split("[", 1)[0]
                column["timestamp"] = (
                    "20260807" if "[20260807]" in new_name else None
                )
        for row in table["datas"]:
            for old_name, new_name in replacements.items():
                row[new_name] = row.pop(old_name)
        table["datas"][0]["股票市场类型"] = [
            "全部A股",
            "沪深主板A股",
            "全部A股",
            " ",
        ]
        self.write_snapshot(self.payload)

        built = build_normalized_tables(self.snapshot, repository_root=self.root)

        security = built["tables"]["security_master"][0]
        valuation = built["tables"]["valuation_snapshots"][0]
        self.assertEqual(
            security["market_memberships"],
            ["全部A股", "沪深主板A股"],
        )
        self.assertEqual(
            security["derived_lineage"]["market_memberships"]["rule"],
            "accept_string_or_string_list_trim_preserve_order_deduplicate",
        )
        self.assertEqual(valuation["pe_ttm"], 6.2)
        self.assertIsNone(valuation["field_lineage"]["pe_ttm"]["as_of_date"])
        self.assertEqual(
            valuation["derived_lineage"]["pe_ttm_as_of_date"]["derived_from"],
            "metadata.as_of_date",
        )
        self.assertEqual(valuation["mapping_version"], "3.4.0")

    def test_rejects_non_string_market_membership_list_items(self) -> None:
        table = self.payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        table["datas"][0]["股票市场类型"] = ["全部A股", 1]
        self.write_snapshot(self.payload)

        with self.assertRaisesRegex(NormalizationError, "only strings"):
            build_normalized_tables(self.snapshot, repository_root=self.root)

    def test_context_free_pe_requires_matching_metadata_date(self) -> None:
        table = self.payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        old_name = "市盈率(pe,ttm)[20260807]"
        new_name = "最新市盈率ttm"
        for column in table["columns"]:
            if column.get("key") == old_name:
                column["key"] = new_name
                column["index_name"] = new_name
                column["timestamp"] = None
        for row in table["datas"]:
            row[new_name] = row.pop(old_name)
        self.write_snapshot(self.payload)
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["metadata"]["as_of_date"] = "2026-08-06"
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")

        with self.assertRaisesRegex(NormalizationError, "share one valid as-of date"):
            build_normalized_tables(self.snapshot, repository_root=self.root)

    def test_preserves_market_cap_when_pe_ttm_is_missing(self) -> None:
        table = self.payload["data"]["answer"][0]["txt"][0]["content"][
            "components"
        ][0]["data"]
        del table["datas"][0]["市盈率(pe,ttm)[20260807]"]
        self.write_snapshot(self.payload)

        built = build_normalized_tables(self.snapshot, repository_root=self.root)

        valuation = built["tables"]["valuation_snapshots"][0]
        self.assertEqual(valuation["market_cap"], 250000000000)
        self.assertIsNone(valuation["pe_ttm"])
        self.assertEqual(
            valuation["field_lineage"]["pe_ttm"]["value_status"],
            "missing_in_source",
        )

    def test_rejects_missing_required_value(self) -> None:
        del self.payload["data"]["answer"][0]["txt"][0]["content"]["components"][0][
            "data"
        ]["datas"][0]["总市值[20260807]"]
        self.write_snapshot(self.payload)

        with self.assertRaisesRegex(NormalizationError, "incomplete valuation"):
            build_normalized_tables(
                self.snapshot, repository_root=self.root
            )

    def test_synthetic_single_page_produces_fifty_records_per_table(self) -> None:
        self.set_minimum_expected_count(50)
        payload = self.synthetic_market_page(
            start=0,
            count=50,
            page=1,
            limit=50,
            total=50,
        )
        snapshot = self.write_snapshot(
            payload,
            path=self.root / "raw" / "page-001.json",
            record_id="synthetic-market-page-001",
            query="synthetic complete market query",
        )

        built = build_normalized_batch(
            [snapshot],
            universe_path=self.universe_path,
            repository_root=self.root,
        )

        self.assertEqual(
            {name: len(records) for name, records in built["tables"].items()},
            {
                "security_master": 50,
                "market_bars_daily": 50,
                "valuation_snapshots": 50,
            },
        )
        self.assertEqual(built["coverage"]["page_count"], 1)
        self.assertEqual(built["coverage"]["expected_page_count"], 1)
        self.assertEqual(built["coverage"]["reported_total_count"], 50)
        self.assertEqual(built["coverage"]["eligible_security_count"], 50)
        self.assertEqual(
            built["tables"]["market_bars_daily"][0]["security_code"],
            "600000.SH",
        )
        self.assertEqual(built["unmapped_fields"], ["自动附加字段[20260807]"])

    def test_full_market_batch_has_complete_unique_security_scope(self) -> None:
        total = 3053
        limit = 50
        self.set_minimum_expected_count(3000)
        raw_root = self.root / "raw" / "full-market"
        snapshots = []
        for page, start in enumerate(range(0, total, limit), start=1):
            count = min(limit, total - start)
            payload = self.synthetic_market_page(
                start=start,
                count=count,
                page=page,
                limit=limit,
                total=total,
            )
            snapshots.append(
                self.write_snapshot(
                    payload,
                    path=raw_root / f"page-{page:03d}.json",
                    record_id=f"synthetic-market-page-{page:03d}",
                    query="synthetic complete market query",
                )
            )

        built = build_normalized_batch(
            list(reversed(snapshots)),
            universe_path=self.universe_path,
            repository_root=self.root,
        )

        self.assertEqual(built["coverage"]["source_snapshot_count"], 62)
        self.assertEqual(built["coverage"]["page_count"], 62)
        self.assertEqual(built["coverage"]["expected_page_count"], 62)
        self.assertEqual(built["coverage"]["reported_total_count"], total)
        self.assertGreaterEqual(built["coverage"]["eligible_security_count"], 3000)
        self.assertEqual(built["coverage"]["minimum_expected_count"], 3000)
        self.assertEqual(
            {name: len(records) for name, records in built["tables"].items()},
            {
                "security_master": total,
                "market_bars_daily": total,
                "valuation_snapshots": total,
            },
        )
        codes = [
            record["security_code"]
            for record in built["tables"]["security_master"]
        ]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertEqual(codes[0], "600000.SH")
        self.assertEqual(codes[-1], "603052.SH")


if __name__ == "__main__":
    unittest.main()
