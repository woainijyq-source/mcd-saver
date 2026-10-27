"""支付宝配置加载。

两个来源，按优先级：

1. 环境变量（支付宝文档「手动集成」标准命名，推荐用于手动接入）：
   AIPAY_APP_ID / AIPAY_PUBLIC_KEY / AIPAY_PRIVATE_KEY /
   AIPAY_PRIVATE_PKCS_KEY / AIPAY_ALIPAY_PUBLIC_KEY / AIPAY_SELLER_ID
2. 项目根目录 `.alipay-sandbox.json`（由 AI 付 Skill 的沙箱配置步骤写入）

`server/.env` 会在进程启动时自动加载（简单 KEY=VALUE 解析，零依赖）。
不读 cwd，不做密钥格式转换：Python 是非 Java 语言，固定用 PKCS#1 私钥。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = PROJECT_ROOT / ".alipay-sandbox.json"
ENV_FILE = Path(__file__).resolve().parent / ".env"

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


def _load_env_file(path: Path) -> None:
    """把 .env 里的键值补进 os.environ（已存在的环境变量优先，不覆盖）。"""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key and key not in os.environ:
            os.environ[key] = value


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


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


def _from_env() -> tuple[str, str, str, str] | None:
    """环境变量齐全时返回 (app_id, private_key, alipay_public_key, seller_id)。"""
    app_id = _env("AIPAY_APP_ID")
    # 非 Java 语言固定取 PKCS#1；只有 PKCS#8 时才用它兜底并提示。
    private_key = _env("AIPAY_PRIVATE_PKCS_KEY") or _env("AIPAY_PRIVATE_KEY")
    alipay_public = _env("AIPAY_ALIPAY_PUBLIC_KEY")
    if not (app_id and private_key and alipay_public):
        return None
    return app_id, private_key, alipay_public, _env("AIPAY_SELLER_ID")


def load_settings() -> AlipaySettings:
    _load_env_file(ENV_FILE)

    env_values = _from_env()
    if env_values is not None:
        app_id, app_private_key, alipay_public_key, seller_id = env_values
        source = "环境变量"
    else:
        if not CONFIG_FILE.is_file():
            raise ConfigError(
                "未找到配置：请设置 AIPAY_* 环境变量（或 server/.env），"
                f"或提供沙箱配置文件 {CONFIG_FILE.name}"
            )
        try:
            payload = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ConfigError("沙箱配置文件不是合法 JSON") from exc
        entry = _first_app(payload)
        app_id = _require(entry, "appId")
        # Python 为非 Java 语言：必须使用 PKCS#1 的 appPrivatePkcsKey 原始值。
        app_private_key = _require(entry, "appPrivatePkcsKey")
        alipay_public_key = _require(entry, "alipayPublicKey")
        seller_id = _env("AIPAY_SELLER_ID") or (entry.get("pid") or "").strip()
        source = CONFIG_FILE.name

    if not seller_id:
        raise ConfigError(f"{source} 缺少商户 ID（pid / AIPAY_SELLER_ID），验付校验需要")

    gateway = _env("ALIPAY_GATEWAY") or SANDBOX_GATEWAY
    if gateway not in (SANDBOX_GATEWAY, PRODUCTION_GATEWAY):
        raise ConfigError("ALIPAY_GATEWAY 只允许沙箱或生产网关")
    service_id = _env("ALIPAY_SERVICE_ID") or SANDBOX_SERVICE_ID
    if gateway == PRODUCTION_GATEWAY and service_id == SANDBOX_SERVICE_ID:
        raise ConfigError("生产网关必须配置服务市场真实 serviceId，不得沿用沙箱 serviceId")

    amount = _env("ALIPAY_PRICE_AMOUNT") or DEFAULT_AMOUNT

    return AlipaySettings(
        app_id=app_id,
        app_private_key=app_private_key,
        alipay_public_key=alipay_public_key,
        seller_id=seller_id,
        gateway=gateway,
        service_id=service_id,
        amount=amount,
    )
