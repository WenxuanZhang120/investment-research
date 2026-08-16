#!/usr/bin/env python3
"""Reject credential and personal-account material from public artifacts."""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any
from urllib.parse import unquote_plus, urlsplit


_CAMEL_CASE_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")

# These are exact credential-bearing names after case/hyphen/camel normalization.
# Broad fragments such as ``account`` or ``session`` suffixes are intentionally
# avoided where they would collide with ordinary market terminology.
CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "authorization",
        "proxy_authorization",
        "cookie",
        "set_cookie",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "authentication_token",
        "session_token",
        "api_key",
        "apikey",
        "password",
        "passwd",
        "credential",
        "credentials",
        "secret",
        "secrets",
        "client_secret",
        "private_key",
        "session",
        "session_id",
        "login_session",
    }
)
CREDENTIAL_FIELD_SUFFIXES = (
    "_password",
    "_passwd",
    "_secret",
    "_cookie",
    "_token",
    "_api_key",
    "_private_key",
    "_session_id",
)

# Only explicit identity/account identifiers are blocked. Normal investment
# fields such as security_code, shareholder_count, accounts_receivable,
# 股票代码 and 股东户数 do not match this exact-name set.
PERSONAL_ACCOUNT_IDENTIFIER_FIELD_NAMES = frozenset(
    {
        "account_id",
        "account_number",
        "account_no",
        "broker_account_id",
        "broker_account_number",
        "brokerage_account_id",
        "brokerage_account_number",
        "securities_account_id",
        "securities_account_number",
        "fund_account_id",
        "fund_account_number",
        "bank_account_id",
        "bank_account_number",
        "bank_card_number",
        "account_holder_name",
        "national_id",
        "national_id_number",
        "id_card_number",
        "phone_number",
        "mobile_number",
        "home_address",
        "residential_address",
        "user_id",
        "customer_id",
        "open_id",
        "union_id",
        "账号",
        "账户号",
        "账户编号",
        "账户号码",
        "资金账号",
        "券商资金账号",
        "证券资金账号",
        "证券账户号",
        "银行卡号",
        "银行卡号码",
        "身份证号",
        "身份证号码",
        "手机号",
        "手机号码",
        "家庭住址",
        "居住地址",
        "用户_id",
        "客户号",
        "客户编号",
    }
)

# A scheme word followed by a long opaque token is high-confidence credential
# material. Ordinary prose such as "Bearer authentication" is shorter and is
# not rejected.
BEARER_CREDENTIAL_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z])bearer[\t ]+[A-Za-z0-9._~+/=-]{16,}"
    r"(?![A-Za-z0-9._~+/=-])"
)

URL_QUERY_PAIR_PATTERN = re.compile(
    r"(?i)(?:&amp;|[?&#])([^=&#\s\"'<>]+)=([^&#\s\"'<>]*)"
)
ABSOLUTE_URL_PATTERN = re.compile(
    r"(?i)\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+"
)
URL_QUERY_CREDENTIAL_FIELD_NAMES = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "auth_token",
        "authentication_token",
        "session_token",
        "api_key",
        "apikey",
        "client_secret",
        "authorization",
        "password",
        "passwd",
        "cookie",
        "x_amz_security_token",
        "x_amz_signature",
        "x_amz_credential",
    }
)

AUTHORIZATION_HEADER_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])[\"']?(?:proxy-)?authorization[\"']?"
    r"[\t ]*:[\t ]*[\"']?[^\s\"'<>;,}]+"
)

# A Basic scheme alone is normal documentation prose.  Candidates are decoded
# below and rejected only when they are valid base64 containing the user/password
# separator required by HTTP Basic credentials.
BASIC_CREDENTIAL_CANDIDATE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z])basic[\t ]+([A-Za-z0-9+/]{4,}={0,2})"
    r"(?![A-Za-z0-9+/=])"
)

# Header-shaped cookie material is high confidence; ordinary phrases such as
# "cookie policy" and "Set-Cookie behavior" do not have a line-leading header
# plus name=value pair and remain publishable.
COOKIE_HEADER_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])[\"']?(?:cookie|set-cookie)[\"']?"
    r"[\t ]*:[\t ]*[\"']?"
    r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+=[^;\s,\r\n]+"
)

# Local filesystem paths must never be copied into public artifacts.  HTTP(S)
# URLs are removed before this check so a legitimate public URL path such as
# ``https://example.com/home/article`` is not mistaken for a local home path.
POSIX_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9._~-])/(?:users|home|tmp|private|var|volumes|opt|etc|"
    r"root|usr|srv|mnt|workspace|app|data|storage|run)"
    r"(?:/|\b)"
)
WINDOWS_LOCAL_PATH_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Z]:[\\/][^\s\"'<>]+"
    r"|\\\\[^\\\s]+\\[^\\\s]+)"
)


class PublicPayloadSafetyError(ValueError):
    """Raised without echoing the sensitive field name or value."""


def _contains_basic_credential(value: str) -> bool:
    for match in BASIC_CREDENTIAL_CANDIDATE_PATTERN.finditer(value):
        candidate = match.group(1)
        padded = candidate + "=" * (-len(candidate) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
        except (binascii.Error, ValueError):
            continue
        if b":" in decoded:
            return True
    return False


def _contains_url_query_credential(value: str) -> bool:
    for match in URL_QUERY_PAIR_PATTERN.finditer(value):
        try:
            key = normalize_field_name(unquote_plus(match.group(1)))
        except (UnicodeError, ValueError):
            continue
        if key in URL_QUERY_CREDENTIAL_FIELD_NAMES and match.group(2):
            return True
    return False


def _contains_url_userinfo(value: str) -> bool:
    for match in ABSOLUTE_URL_PATTERN.finditer(value):
        candidate = match.group(0).rstrip(".,);]")
        try:
            parsed = urlsplit(candidate)
            if parsed.username not in (None, "") or parsed.password not in (None, ""):
                return True
        except (UnicodeError, ValueError):
            continue
    return False


def _contains_forbidden_local_path(value: str) -> bool:
    visible_parts = []
    cursor = 0
    for match in ABSOLUTE_URL_PATTERN.finditer(value):
        visible_parts.append(value[cursor : match.start()])
        candidate = match.group(0).rstrip(".,);]")
        try:
            scheme = urlsplit(candidate).scheme.casefold()
        except (UnicodeError, ValueError):
            scheme = ""
        if scheme == "file":
            return True
        if scheme not in {"http", "https"}:
            visible_parts.append(match.group(0))
        cursor = match.end()
    visible_parts.append(value[cursor:])
    non_http_text = " ".join(visible_parts)
    return bool(
        POSIX_LOCAL_PATH_PATTERN.search(non_http_text)
        or WINDOWS_LOCAL_PATH_PATTERN.search(non_http_text)
    )


def normalize_field_name(value: Any) -> str:
    """Normalize common JSON/CSV field-name styles for exact matching."""
    text = _CAMEL_CASE_BOUNDARY.sub("_", str(value).strip())
    return re.sub(r"[-\s]+", "_", text).casefold()


def forbidden_field_category(value: Any) -> str | None:
    """Return a safe category label when a field must not be public."""
    normalized = normalize_field_name(value)
    if normalized in CREDENTIAL_FIELD_NAMES or normalized.endswith(
        CREDENTIAL_FIELD_SUFFIXES
    ):
        return "credential field"
    if normalized in PERSONAL_ACCOUNT_IDENTIFIER_FIELD_NAMES:
        return "personal/account identifier field"
    return None


def assert_public_field_names_safe(
    field_names: Any, *, location: str = "$.*"
) -> None:
    """Reject sensitive names only inside an explicit schema-name container."""
    if not isinstance(field_names, (list, tuple, set, frozenset)):
        return
    for field_name in field_names:
        if not isinstance(field_name, str):
            continue
        category = forbidden_field_category(field_name)
        if category is not None:
            raise PublicPayloadSafetyError(
                f"public payload declares a forbidden {category} at {location}[]"
            )


def _assert_column_descriptor_names_safe(
    descriptors: Any, *, location: str
) -> None:
    """Inspect only explicit ``columns[]`` key/index_name descriptors."""
    if not isinstance(descriptors, list):
        return
    for descriptor in descriptors:
        if not isinstance(descriptor, dict):
            continue
        for descriptor_key, field_name in descriptor.items():
            if normalize_field_name(descriptor_key) not in {"key", "index_name"}:
                continue
            if not isinstance(field_name, str):
                continue
            category = forbidden_field_category(field_name)
            if category is not None:
                raise PublicPayloadSafetyError(
                    "public payload declares a forbidden "
                    f"{category} at {location}[].*"
                )


def assert_public_payload_safe(value: Any, *, location: str = "$") -> None:
    """Recursively reject unsafe public data without exposing its contents."""
    if isinstance(value, dict):
        for key, child in value.items():
            normalized_key = normalize_field_name(key)
            category = forbidden_field_category(normalized_key)
            if category is not None:
                raise PublicPayloadSafetyError(
                    f"public payload contains a forbidden {category} at {location}.*"
                )
            if normalized_key == "raw_field_names":
                assert_public_field_names_safe(
                    child, location=f"{location}.*"
                )
            elif normalized_key == "columns":
                _assert_column_descriptor_names_safe(
                    child, location=f"{location}.*"
                )
            assert_public_payload_safe(child, location=f"{location}.*")
        return
    if isinstance(value, list):
        for child in value:
            assert_public_payload_safe(child, location=f"{location}[]")
        return
    if isinstance(value, str):
        if BEARER_CREDENTIAL_PATTERN.search(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden Bearer credential value at {location}"
            )
        if _contains_url_query_credential(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden URL credential query value at {location}"
            )
        if _contains_url_userinfo(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden URL userinfo credential at {location}"
            )
        if _contains_basic_credential(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden Basic credential value at {location}"
            )
        if AUTHORIZATION_HEADER_PATTERN.search(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden Authorization header value at {location}"
            )
        if COOKIE_HEADER_PATTERN.search(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden Cookie header value at {location}"
            )
        if _contains_forbidden_local_path(value):
            raise PublicPayloadSafetyError(
                f"public payload contains a forbidden local filesystem path at {location}"
            )
