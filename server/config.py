"""支付宝配置加载。

唯一可信来源是项目根目录的 `.alipay-sandbox.json`（由沙箱配置步骤写入并已校验）。
不依赖 cwd，不读取环境变量中的密钥，不做任何密钥格式转换：
Python SDK 使用 PKCS#1 的 `appPrivatePkcsKey` 原始值。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / ".alipay-sandbox.json"

SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
PRODUCTION_GATEWAY = "https://openapi.alipay.com/gateway.do"
SANDBOX_SERVICE_ID = "api_mock_service_id"

DEFAULT_AMOUNT = "0.01"
DEFAULT_ORDER_TTL_MINUTES = 30


class ConfigError(RuntimeError):
    """配置缺失或字段不可用。宁可明确失败，也不回退示例值或生产网关。"""


@dataclass(frozen=True)
class AlipaySettings:
    app_id: str
    app_private_key: str
    alipay_public_key: str
    seller_id: str
    gateway: str
    service_id: str
    seller_name: str = "麦麦省钱助手"
    amount: str = DEFAULT_AMOUNT
    order_ttl_minutes: int = DEFAULT_ORDER_TTL_MINUTES

    @property
    def is_exact_sandbox(self) -> bool:
        return self.gateway == SANDBOX_GATEWAY and self.service_id == SANDBOX_SERVICE_ID


def _first_app(payload: Any) -> dict[str, Any]:
    apps = payload.get("appIds") if isinstance(payload, dict) else None
    if not isinstance(apps, list) or not apps:
        raise ConfigError("沙箱配置缺少 appIds")
    for entry in apps:
        if isinstance(entry, dict) and entry.get("appId"):
            return entry
    raise ConfigError("沙箱配置 appIds 中没有可用的应用条目")


def _require(entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"沙箱配置缺少字段 {key}")
    return value.strip()


def load_settings() -> AlipaySettings:
    if not CONFIG_FILE.is_file():
        raise ConfigError(f"未找到沙箱配置文件: {CONFIG_FILE.name}")
    try:
        payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError("沙箱配置文件不是合法 JSON") from exc

    entry = _first_app(payload)

    # Python 为非 Java 语言：必须使用 PKCS#1 的 appPrivatePkcsKey 原始值。
    app_private_key = _require(entry, "appPrivatePkcsKey")
    alipay_public_key = _require(entry, "alipayPublicKey")
    app_id = _require(entry, "appId")
    seller_id = _require(entry, "pid")

    gateway = os.environ.get("ALIPAY_GATEWAY", SANDBOX_GATEWAY).strip() or SANDBOX_GATEWAY
    if gateway not in (SANDBOX_GATEWAY, PRODUCTION_GATEWAY):
        raise ConfigError("ALIPAY_GATEWAY 只允许沙箱或生产网关")
    service_id = os.environ.get("ALIPAY_SERVICE_ID", SANDBOX_SERVICE_ID).strip() or SANDBOX_SERVICE_ID
    if gateway == PRODUCTION_GATEWAY and service_id == SANDBOX_SERVICE_ID:
        raise ConfigError("生产网关必须配置服务市场真实 serviceId，不得沿用沙箱 serviceId")

    amount = os.environ.get("ALIPAY_PRICE_AMOUNT", DEFAULT_AMOUNT).strip() or DEFAULT_AMOUNT

    return AlipaySettings(
        app_id=app_id,
        app_private_key=app_private_key,
        alipay_public_key=alipay_public_key,
        seller_id=seller_id,
        gateway=gateway,
        service_id=service_id,
        amount=amount,
    )
