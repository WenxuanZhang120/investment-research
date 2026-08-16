import sys
import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from scripts.public_payload_safety import (  # noqa: E402
    PublicPayloadSafetyError,
    assert_public_payload_safe,
)


class PublicPayloadSafetyTests(unittest.TestCase):
    def test_rejects_nested_credential_fields_without_echoing_contents(self):
        field_names = (
            "Authorization",
            "Cookie",
            "Set-Cookie",
            "token",
            "accessToken",
            "session",
            "sessionId",
            "privateKey",
            "password",
            "X-Api-Key",
        )
        for field_name in field_names:
            with self.subTest(field_name=field_name):
                marker = "test-only-sensitive-marker"
                with self.assertRaises(PublicPayloadSafetyError) as raised:
                    assert_public_payload_safe(
                        {"outer": [{"metadata": {field_name: marker}}]}
                    )
                self.assertIn("credential field", str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))
                self.assertNotIn(field_name, str(raised.exception))

    def test_rejects_explicit_personal_and_account_identifier_fields(self):
        for field_name in (
            "accountId",
            "brokerage_account_number",
            "bank_card_number",
            "id_card_number",
            "mobile_number",
            "资金账号",
            "身份证号",
            "手机号",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaisesRegex(
                    PublicPayloadSafetyError,
                    "personal/account identifier field",
                ):
                    assert_public_payload_safe({field_name: "redacted"})

    def test_rejects_high_confidence_bearer_value_without_echoing_it(self):
        marker = "Bearer test_only_opaque_token_123456789"
        with self.assertRaises(PublicPayloadSafetyError) as raised:
            assert_public_payload_safe({"headers_text": marker})
        self.assertIn("Bearer credential value", str(raised.exception))
        self.assertNotIn(marker, str(raised.exception))

    def test_rejects_sensitive_names_in_explicit_schema_containers(self):
        for payload in (
            {"raw_field_names": ["股票代码", "Authorization"]},
            {"columns": [{"key": "token", "title": "opaque"}]},
            {"columns": [{"index_name": "资金账号"}]},
        ):
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(
                    PublicPayloadSafetyError,
                    "declares a forbidden",
                ):
                    assert_public_payload_safe(payload)

    def test_allows_normal_market_and_deidentified_holding_fields(self):
        assert_public_payload_safe(
            {
                "security_code": "000001.SZ",
                "shareholder_count": 12345,
                "accounts_receivable": 100,
                "trading_session": "regular",
                "股票代码": "000001.SZ",
                "股东户数": 12345,
                "应收账款": 100,
                "holding": {
                    "quantity": 10,
                    "average_cost": 9.5,
                    "market_value": 100,
                    "target_weight": 0.1,
                },
            }
        )

    def test_does_not_treat_bearer_prose_as_a_credential(self):
        assert_public_payload_safe(
            {
                "documentation": "Bearer authentication is not stored here.",
                "news": {"key": "token", "summary": "Token economy policy"},
            }
        )


if __name__ == "__main__":
    unittest.main()
