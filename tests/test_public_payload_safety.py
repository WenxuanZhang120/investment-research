import base64
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

    def test_rejects_url_query_credential_values_without_echoing_them(self):
        markers = (
            "https://example.invalid/data?token=test-only-secret",
            "https://example.invalid/data?symbol=000001&access_token=test-only-secret",
            "https://example.invalid/data?api_key=test-only-secret",
            "https://example.invalid/data?topic=market&amp;auth-token=test-only-secret",
            "https://example.invalid/data?access%5Ftoken=test-only-secret",
            "https://example.invalid/data?X-Amz-Security-Token=test-only-secret",
            "https://example.invalid/data?X-Amz-Signature=test-only-secret",
            "https://example.invalid/callback#access_token=test-only-secret",
            "https://example.invalid/callback#token=test-only-secret",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(PublicPayloadSafetyError) as raised:
                    assert_public_payload_safe({"source_url": marker})
                self.assertIn("URL credential query value", str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))

    def test_rejects_decodable_basic_authorization_without_echoing_it(self):
        encoded = base64.b64encode(b"test-user:test-password").decode("ascii")
        marker = f"Authorization: Basic {encoded}"
        with self.assertRaises(PublicPayloadSafetyError) as raised:
            assert_public_payload_safe({"headers_text": marker})
        self.assertIn("Basic credential value", str(raised.exception))
        self.assertNotIn(marker, str(raised.exception))

    def test_rejects_other_nonempty_authorization_header_values(self):
        for marker in (
            'prefix Authorization: Digest username="test-user",response="secret" suffix',
            'const headers={"Authorization":"Digest test-only-secret"};',
        ):
            with self.subTest(marker=marker):
                with self.assertRaises(PublicPayloadSafetyError) as raised:
                    assert_public_payload_safe({"diagnostic": marker})
                self.assertIn("Authorization header value", str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))

    def test_rejects_url_userinfo_without_echoing_it(self):
        marker = "https://test-user:test-password@example.invalid/market"
        with self.assertRaises(PublicPayloadSafetyError) as raised:
            assert_public_payload_safe({"source_url": marker})
        self.assertIn("URL userinfo credential", str(raised.exception))
        self.assertNotIn(marker, str(raised.exception))

    def test_rejects_cookie_header_shapes_without_echoing_them(self):
        for marker in (
            "Cookie: sessionid=test-only-secret; theme=dark",
            "HTTP/1.1 200 OK\nSet-Cookie: auth=test-only-secret; Path=/; HttpOnly",
            'const h="prefix Cookie: sessionid=test-only-secret; suffix";',
            'const h="prefix Set-Cookie: auth=test-only-secret;Path=/";',
            'const h={"Cookie":"sessionid=test-only-secret"};',
            'const h={"Set-Cookie":"auth=test-only-secret"};',
        ):
            with self.subTest(marker=marker):
                with self.assertRaises(PublicPayloadSafetyError) as raised:
                    assert_public_payload_safe({"headers_text": marker})
                self.assertIn("Cookie header value", str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))

    def test_rejects_local_and_private_paths_without_echoing_them(self):
        markers = (
            "/Users/test-user/repository/report.json",
            "/home/test-user/report.json",
            "/tmp/report.json",
            "/private/tmp/report.json",
            "/var/folders/test/report.json",
            "/Volumes/research/report.json",
            "/opt/research/report.json",
            "/etc/passwd",
            r"C:\work\repository\report.json",
            r"D:/work/repository/report.json",
            "file:///tmp/report.json",
            "/private/var/folders/test/report.json",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                with self.assertRaises(PublicPayloadSafetyError) as raised:
                    assert_public_payload_safe({"note": marker})
                self.assertIn("local filesystem path", str(raised.exception))
                self.assertNotIn(marker, str(raised.exception))

    def test_allows_repository_relative_lineage_paths_for_non_site_callers(self):
        assert_public_payload_safe(
            {
                "raw_lineage": "data/raw/2026/08/16/response.json",
                "deidentified_holdings": "portfolio/holdings.csv",
                "journal": "decision_journal/2026-08-16.md",
            }
        )

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

    def test_allows_ordinary_finance_urls_and_security_prose(self):
        assert_public_payload_safe(
            {
                "urls": [
                    "https://finance.example/quote?symbol=TOKEN&market=US",
                    "https://finance.example/article?topic=access_token",
                    "https://finance.example/article?api_keynote=valuation",
                    "https://finance.example/article?token=",
                    "https://finance.example/article?signature_method=sha256",
                    "https://finance.example/article?X-Amz-Algorithm=AWS4-HMAC-SHA256",
                    "https://finance.example/research/user@example.com",
                    "https://finance.example/home/article",
                    "https://finance.example/tmp/public-note",
                    "https://finance.example/Volumes/public-report",
                    "https://finance.example/article#topic=token",
                ],
                "documentation": [
                    "Basic financial analysis compares revenue and cash flow.",
                    "Cookie policy changes can affect advertising companies.",
                    "Set-Cookie behavior is discussed without any header value.",
                    "Authorization header documentation is available publicly.",
                    "Token economy policy remains a market topic.",
                ],
            }
        )


if __name__ == "__main__":
    unittest.main()
