"""账单签名与编码工具。

账单签名字段固定为字典序：amount、currency、goods_name、out_trade_no、
pay_before、resource_id、seller_id、service_id。签名只在商家本地完成。
"""

from __future__ import annotations

import base64
import json
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from Crypto.Signature import pkcs1_15

BILL_SIGN_FIELDS = (
    "amount",
    "currency",
    "goods_name",
    "out_trade_no",
    "pay_before",
    "resource_id",
    "seller_id",
    "service_id",
)

_AMOUNT_RE = re.compile(r"\d+(?:\.\d{1,2})?")


def normalize_amount(value: Any) -> str | None:
    text = str(value or "").strip()
    if not _AMOUNT_RE.fullmatch(text):
        return None
    try:
        return f"{Decimal(text):.2f}"
    except InvalidOperation:
        return None


def amounts_equal(left: Any, right: Any) -> bool:
    left_amount = normalize_amount(left)
    right_amount = normalize_amount(right)
    return left_amount is not None and left_amount == right_amount


def base64url_encode(data: Any) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def base64url_decode(data: str) -> str:
    padding = 4 - len(data) % 4
    if padding != 4:
        data += "=" * padding
    return base64.urlsafe_b64decode(data).decode("utf-8")


def _signing_content(fields: Mapping[str, Any]) -> str:
    parts = [f"{key}={fields[key]}" for key in BILL_SIGN_FIELDS if fields.get(key) not in (None, "")]
    return "&".join(parts)


def sign_bill(fields: Mapping[str, Any], private_key: str) -> str:
    """用商家私钥对账单字段做 RSA2 签名。

    私钥只在内存中临时解码为密钥对象用于签名，不回写配置、不做格式转换。
    """
    content = _signing_content(fields)
    # 签名内容直接用已校验的配置取值，避免调用方传入缺字段导致签名错位。
    raw_key = "".join(private_key.split())
    key = RSA.import_key(base64.b64decode(raw_key))
    digest = SHA256.new(content.encode("utf-8"))
    return base64.b64encode(pkcs1_15.new(key).sign(digest)).decode("utf-8")


def decode_payment_proof(payment_proof: str) -> dict[str, Any]:
    """解码 Payment-Proof，返回已提取的验付入参。"""
    payload = json.loads(base64url_decode(payment_proof))
    protocol = payload.get("protocol") or {}
    method = payload.get("method") or {}
    return {
        "payment_proof": protocol.get("payment_proof"),
        "trade_no": protocol.get("trade_no"),
        "client_session": method.get("client_session"),
    }
