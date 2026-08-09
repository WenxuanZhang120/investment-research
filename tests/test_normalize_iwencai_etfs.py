import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.normalize_iwencai_etfs import (  # noqa: E402
    EtfNormalizationError,
    build_etf_batch,
    build_etf_tables,
    write_etf_bundle,
)


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


class NormalizeIwencaiEtfTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.snapshot = self.root / "data/raw/iwencai/2026/08/09/etf.json"
        self.snapshot.parent.mkdir(parents=True)
        self.payload = {
            "success": True,
            "page": 1,
            "limit": 100,
            "code_count": 2,
            "datas": [
                {
                    "ETF代码": "513100.SH",
                    "ETF简称": "纳指ETF",
                    "跟踪指数": "纳斯达克100指数",
                    "基金类型": "跨境ETF",
                    "上市日期": "20130515",
                    "上市状态": "正常上市",
                    "最新价": 1.5,
                    "涨跌幅": 1.2,
                    "成交量": 100,
                    "成交额": 150.0,
                    "基金规模": 1000.0,
                    "单位净值": 1.49,
                    "溢价率": 0.67,
                    "管理费率": 0.5,
                    "托管费率": 0.1,
                    "跟踪误差": 0.25,
                },
                {
                    "ETF代码": "159655.SZ",
                    "ETF简称": "标普ETF",
                    "跟踪指数": "标普500指数",
                    "基金类型": "QDII-ETF",
                    "上市日期": "20230320",
                    "上市状态": "正常上市",
                    "最新价": 1.2,
                    "涨跌幅": -0.1,
                    "成交量": 200,
                    "成交额": 240.0,
                    "基金规模": 800.0,
                    "单位净值": 1.2,
                    "溢价率": 0.0,
                    "管理费率": 0.5,
                    "托管费率": 0.1,
                    "跟踪误差": 0.3,
                },
            ],
        }
        self.write_snapshot(self.payload)

    def write_snapshot(self, payload):
        envelope = {
            "metadata": {
                "source": "iwencai",
                "query": "境内纳指与标普ETF测试",
                "fetched_at": "2026-08-09T19:00:00+08:00",
                "as_of_date": "2026-08-09",
                "record_id": "dummy-etf-record",
                "payload_sha256": hashlib.sha256(canonical(payload)).hexdigest(),
            },
            "payload": payload,
        }
        self.snapshot.write_text(
            json.dumps(envelope, ensure_ascii=False), encoding="utf-8"
        )

    def test_serializes_configured_etfs_with_raw_field_lineage(self):
        built = build_etf_batch([self.snapshot], repository_root=self.root)
        self.assertEqual(built["coverage"]["etf_count"], 2)
        self.assertEqual(
            built["coverage"]["tracked_index_family_counts"],
            {"nasdaq_100": 1, "sp_500": 1},
        )
        self.assertEqual(
            built["records"][0]["raw_snapshot"],
            "data/raw/iwencai/2026/08/09/etf.json",
        )
        self.assertEqual(
            built["records"][0]["field_lineage"]["etf_code"]["raw_field_name"],
            "ETF代码",
        )
        destination = write_etf_bundle(
            built, normalized_root=self.root / "data/normalized"
        )
        manifest = json.loads((destination / "manifest.json").read_text())
        self.assertEqual(manifest["universe_id"], "cn_listed_nasdaq_sp500_etfs")
        self.assertEqual(manifest["tables"]["etf_snapshots"]["record_count"], 2)

    def test_rejects_etf_outside_configured_index_scope(self):
        payload = json.loads(json.dumps(self.payload, ensure_ascii=False))
        payload["datas"] = [payload["datas"][0]]
        payload["datas"][0]["跟踪指数"] = "沪深300指数"
        payload["code_count"] = 1
        self.write_snapshot(payload)
        with self.assertRaisesRegex(EtfNormalizationError, "outside configured scope"):
            build_etf_tables(self.snapshot, repository_root=self.root)

    def test_rejects_tampered_raw_snapshot(self):
        envelope = json.loads(self.snapshot.read_text(encoding="utf-8"))
        envelope["payload"]["datas"][0]["最新价"] = 99
        self.snapshot.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(EtfNormalizationError, "checksum"):
            build_etf_tables(self.snapshot, repository_root=self.root)


if __name__ == "__main__":
    unittest.main()
