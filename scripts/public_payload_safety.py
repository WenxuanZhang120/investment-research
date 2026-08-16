#!/usr/bin/env python3
"""Reject credential and personal-account material from public artifacts."""

from __future__ import annotations

import re
from typing import Any


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


class PublicPayloadSafetyError(ValueError):
    """Raised without echoing the sensitive field name or value."""


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
    if isinstance(value, str) and BEARER_CREDENTIAL_PATTERN.search(value):
        raise PublicPayloadSafetyError(
            f"public payload contains a forbidden Bearer credential value at {location}"
        )
