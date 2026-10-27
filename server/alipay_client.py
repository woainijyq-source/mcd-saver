"""支付宝 SDK 调用封装：验付与履约确认。

只使用已验证的 SDK 执行方法（服务端接口用 execute），不存在跳过验签或固定成功返回的分支。
"""

from __future__ import annotations

import json
from typing import Any

from .config import AlipaySettings

VERIFY_API = "alipay.aipay.agent.payment.verify"
CONFIRM_API = "alipay.aipay.agent.fulfillment.confirm"


class AlipayCallError(RuntimeError):
    """SDK 调用失败或返回结构不可用。"""


def build_client(settings: AlipaySettings):
    from alipay.aop.api.AlipayClientConfig import AlipayClientConfig
    from alipay.aop.api.DefaultAlipayClient import DefaultAlipayClient

    config = AlipayClientConfig()
    config.server_url = settings.gateway
    config.app_id = settings.app_id
    # Python 使用沙箱配置中的 PKCS#1 原始私钥，不做格式转换。
    config.app_private_key = settings.app_private_key
    config.alipay_public_key = settings.alipay_public_key
    config.charset = "utf-8"
    config.sign_type = "RSA2"
    return DefaultAlipayClient(alipay_client_config=config)


def _loads(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    if not isinstance(payload, str) or not payload.strip():
        raise AlipayCallError("SDK 返回内容为空")
    data = json.loads(payload)
    if not isinstance(data, dict):
        raise AlipayCallError("SDK 返回内容不是 JSON 对象")
    return data


def _pick(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def verify_payment(
    settings: AlipaySettings,
    trade_no: str,
    payment_proof: str,
    client_session: str | None = None,
) -> dict[str, Any]:
    """调用验付接口，返回扁平化后的关键字段。"""
    from alipay.aop.api.domain.AlipayAipayAgentPaymentVerifyModel import AlipayAipayAgentPaymentVerifyModel
    from alipay.aop.api.request.AlipayAipayAgentPaymentVerifyRequest import AlipayAipayAgentPaymentVerifyRequest

    client = build_client(settings)
    request = AlipayAipayAgentPaymentVerifyRequest()
    model = AlipayAipayAgentPaymentVerifyModel()
    model.payment_proof = payment_proof
    model.trade_no = trade_no
    if client_session:
        model.client_session = client_session
    request.biz_model = model

    raw = _loads(client.execute(request))
    data = raw.get("alipay_aipay_agent_payment_verify_response", raw)
    return {
        "code": data.get("code"),
        "sub_code": data.get("sub_code"),
        "sub_msg": data.get("sub_msg"),
        "active": data.get("active"),
        "trade_no": _pick(data, "trade_no", "tradeNo"),
        "out_trade_no": _pick(data, "out_trade_no", "outTradeNo"),
        "amount": data.get("amount"),
        "resource_id": _pick(data, "resource_id", "resourceId"),
    }


def confirm_fulfillment(settings: AlipaySettings, trade_no: str) -> tuple[bool, str]:
    """调用履约确认接口。返回 (是否成功, 失败原因)。"""
    from alipay.aop.api.domain.AlipayAipayAgentFulfillmentConfirmModel import (
        AlipayAipayAgentFulfillmentConfirmModel,
    )
    from alipay.aop.api.request.AlipayAipayAgentFulfillmentConfirmRequest import (
        AlipayAipayAgentFulfillmentConfirmRequest,
    )

    if not trade_no:
        return False, "trade_no 为空"

    client = build_client(settings)
    request = AlipayAipayAgentFulfillmentConfirmRequest()
    model = AlipayAipayAgentFulfillmentConfirmModel()
    model.trade_no = trade_no
    request.biz_model = model

    raw = _loads(client.execute(request))
    data = raw.get("alipay_aipay_agent_fulfillment_confirm_response", raw)
    if data.get("code") == "10000":
        return True, ""
    return False, str(data.get("sub_msg") or data.get("msg") or "履约确认未返回成功")
