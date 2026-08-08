import json
import sys
import tempfile
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.audit_iwencai_response import audit_snapshot, write_report  # noqa: E402


class AuditIwencaiResponseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_path.write_text(
            json.dumps(
                {
                    "metadata": {
                        "source": "iwencai",
                        "query": "test query",
                        "fetched_at": "2026-08-08T17:00:00.000000+08:00",
                        "record_id": "test-record",
                    },
                    "payload": {
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
                                                                    "key": "收盘价:不复权[20260807]",
                                                                    "index_name": "收盘价:不复权",
                                                                    "source": "new_parser",
                                                                    "type": "DOUBLE",
                                                                    "unit": "元",
                                                                    "timestamp": "20260807",
                                                                },
                                                                {
                                                                    "key": "未知指标[最新]",
                                                                    "index_name": "未知指标",
                                                                    "source": "add_condition",
                                                                    "type": "DOUBLE",
                                                                    "unit": "",
                                                                },
                                                            ],
                                                            "datas": [{"股票代码": "000001"}],
                                                            "meta": {"extra": {"code_count": 100}},
                                                        }
                                                    }
                                                ]
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_audits_real_response_shape(self) -> None:
        report = audit_snapshot(self.snapshot_path)

        self.assertEqual(report["summary"]["column_count"], 3)
        self.assertEqual(report["summary"]["mapped_column_count"], 2)
        self.assertEqual(report["summary"]["unmapped_column_count"], 1)
        self.assertEqual(report["summary"]["returned_row_count"], 1)
        self.assertEqual(report["summary"]["reported_total_count"], 100)
        self.assertEqual(report["columns"][1]["canonical_field_name"], "close")
        self.assertEqual(report["columns"][1]["adjustment_type"], "unadjusted")
        self.assertEqual(report["columns"][1]["source_unit"], "元")

    def test_report_writer_refuses_overwrite(self) -> None:
        output_path = self.root / "report.json"
        write_report({"status": "ok"}, output_path)

        with self.assertRaises(FileExistsError):
            write_report({"status": "changed"}, output_path)

    def test_audits_pagination_response_shape(self) -> None:
        document = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        component = document["payload"]["data"]["answer"][0]["txt"][0][
            "content"
        ]["components"][0]
        document["payload"] = {"answer": {"components": [component]}}
        self.snapshot_path.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )

        report = audit_snapshot(self.snapshot_path)

        self.assertEqual(report["summary"]["table_component_count"], 1)
        self.assertEqual(report["summary"]["column_count"], 3)
        self.assertEqual(report["summary"]["reported_total_count"], 100)

    def test_audits_direct_openapi_response_shape(self) -> None:
        document = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
        document["payload"] = {
            "code_count": 1,
            "page": 1,
            "limit": 10,
            "datas": [
                {
                    "股票代码": "600519.SH",
                    "股票简称": "贵州茅台",
                    "公告日期[20251231]": "20260417",
                    "报告期[20251231]": "2025年年报",
                    "营业收入[20251231]": 168838102514.79,
                }
            ],
        }
        self.snapshot_path.write_text(
            json.dumps(document, ensure_ascii=False),
            encoding="utf-8",
        )

        report = audit_snapshot(self.snapshot_path)

        self.assertEqual(report["summary"]["table_component_count"], 1)
        self.assertEqual(report["summary"]["column_count"], 5)
        self.assertEqual(report["summary"]["mapped_column_count"], 5)
        self.assertEqual(report["summary"]["returned_row_count"], 1)
        revenue = next(
            column
            for column in report["columns"]
            if column["canonical_field_name"] == "revenue"
        )
        self.assertEqual(revenue["period_end"], "2025-12-31")
        self.assertEqual(revenue["source_role"], "iwencai_openapi")


if __name__ == "__main__":
    unittest.main()
