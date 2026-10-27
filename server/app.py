"""AI 按量付费（A2M）服务端入口。

协议：
1. 请求不带 `Payment-Proof` → 402 + `Payment-Needed`（Base64URL 账单，商家签名）；
2. 请求带 `Payment-Proof` → 验付 → 生成资源（仅一次）→ 履约确认 → 返回资源。

任何验付校验不通过都回到 402，不返回资源。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Flask, Response, request

from .alipay_client import confirm_fulfillment, verify_payment
from .config import PROJECT_ROOT, AlipaySettings, ConfigError, load_settings
from .order_repository import DEFAULT_DB_PATH, OrderError, SqliteOrderRepository
from .plan_service import GOODS_NAME, RESOURCE_PATH, QueryError, build_plan_content, parse_query
from .security import (
    amounts_equal,
    base64url_encode,
    decode_payment_proof,
    normalize_amount,
    sign_bill,
)

RESOURCE_METHODS = ["GET", "POST"]


def _debug(tag: str, payload: dict[str, Any]) -> None:
    """诊断钩子：仅在 MCD_A2M_DEBUG=1 时把非敏感结论写入本地文件。"""
    if os.environ.get("MCD_A2M_DEBUG") != "1":
        return
    path = PROJECT_ROOT / "server" / "var" / "a2m-debug.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"tag": tag, **payload}, ensure_ascii=False) + "\n")


def _json_response(payload: dict[str, Any], status: int = 200, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(payload, ensure_ascii=False)
    response = Response(body, status=status, mimetype="application/json; charset=utf-8")
    for key, value in (headers or {}).items():
        response.headers[key] = value
    return response


def _request_params() -> dict[str, Any]:
    params: dict[str, Any] = {key: value for key, value in request.args.items()}
    if request.method == "POST":
        body = request.get_json(silent=True)
        if isinstance(body, dict):
            params.update({str(key): value for key, value in body.items()})
    return params


def create_app(settings: AlipaySettings | None = None, repository: SqliteOrderRepository | None = None) -> Flask:
    app = Flask(__name__)
    app.config["ALIPAY_SETTINGS"] = settings or load_settings()
    app.config["ORDER_REPOSITORY"] = repository or SqliteOrderRepository(DEFAULT_DB_PATH)

    def current_settings() -> AlipaySettings:
        return app.config["ALIPAY_SETTINGS"]

    def current_repository() -> SqliteOrderRepository:
        return app.config["ORDER_REPOSITORY"]

    @app.get("/healthz")
    def healthz() -> Response:
        return _json_response({"status": "ok"})

    @app.route(RESOURCE_PATH, methods=RESOURCE_METHODS)
    def resource() -> Response:
        proof = (request.headers.get("Payment-Proof") or "").strip()
        if proof:
            return _verify_and_deliver(proof, current_settings(), current_repository())
        try:
            params = parse_query(_request_params())
        except QueryError as exc:
            return _json_response({"code": "INVALID_QUERY", "message": str(exc)}, status=400)
        return _payment_required(params, current_settings(), current_repository())

    return app


def _payment_required(
    params: dict[str, Any],
    settings: AlipaySettings,
    repository: SqliteOrderRepository,
) -> Response:
    amount = normalize_amount(settings.amount)
    if amount is None:
        return _json_response({"code": "CONFIG_ERROR", "message": "计价金额配置不合法"}, status=500)

    out_trade_no = f"ORDER_{int(time.time() * 1000)}_{uuid.uuid4().hex[:12]}"
    pay_before = (datetime.now(timezone.utc).astimezone() + timedelta(minutes=settings.order_ttl_minutes)).isoformat()

    bill_fields = {
        "amount": amount,
        "currency": "CNY",
        "goods_name": GOODS_NAME,
        "out_trade_no": out_trade_no,
        "pay_before": pay_before,
        "resource_id": RESOURCE_PATH,
        "seller_id": settings.seller_id,
        "service_id": settings.service_id,
    }
    try:
        seller_signature = sign_bill(bill_fields, settings.app_private_key)
    except Exception:
        return _json_response({"code": "SIGN_ERROR", "message": "账单签名失败"}, status=500)

    repository.create_pending(
        {
            "out_trade_no": out_trade_no,
            "amount": amount,
            "currency": "CNY",
            "resource_id": RESOURCE_PATH,
            "goods_name": GOODS_NAME,
            "pay_before": pay_before,
            "order_status": "PENDING_PAYMENT",
            "fulfill_status": "UNFULFILLED",
            "query": {
                "budget": params["budget"],
                "calories_max": params["calories_max"],
                "channel": params["channel"].value,
                "time": params["time"].strftime("%H:%M") if params["time"] else None,
                "goal": params["goal"].value if params["goal"] else None,
                "must_include": list(params["must_include"]),
                "delivery_fee": params["delivery_fee"],
            },
        }
    )

    payment_needed = {
        "protocol": {
            "out_trade_no": out_trade_no,
            "amount": amount,
            "currency": "CNY",
            "resource_id": RESOURCE_PATH,
            "pay_before": pay_before,
            "seller_signature": seller_signature,
            "seller_sign_type": "RSA2",
            "seller_unique_id": settings.seller_id,
        },
        "method": {
            "seller_name": settings.seller_name,
            "seller_id": settings.seller_id,
            "seller_app_id": settings.app_id,
            "goods_name": GOODS_NAME,
            "seller_unique_id_key": "seller_id",
            "service_id": settings.service_id,
        },
    }

    return _json_response(
        {
            "code": "Payment-Needed",
            "message": "需要支付后获取点单方案",
            "out_trade_no": out_trade_no,
            "amount": amount,
            "currency": "CNY",
            "goods_name": GOODS_NAME,
        },
        status=402,
        headers={"Payment-Needed": base64url_encode(json.dumps(payment_needed, ensure_ascii=False))},
    )


def _verify_and_deliver(
    proof: str,
    settings: AlipaySettings,
    repository: SqliteOrderRepository,
) -> Response:
    try:
        decoded = decode_payment_proof(proof)
    except Exception:
        return _payment_required_from_empty(settings, repository)

    payment_proof_value = (decoded.get("payment_proof") or "").strip()
    trade_no = (decoded.get("trade_no") or "").strip()
    if not payment_proof_value or not trade_no:
        return _payment_required_from_empty(settings, repository)

    try:
        result = verify_payment(settings, trade_no, payment_proof_value, decoded.get("client_session"))
    except Exception:
        return _json_response(
            {"code": "VERIFY_CALL_FAILED", "message": "验付接口调用失败，请稍后用同一 Payment-Proof 重试"},
            status=502,
        )

    if result.get("code") != "10000":
        _debug("verify_code", {"code": result.get("code"), "sub_code": result.get("sub_code"), "sub_msg": result.get("sub_msg")})
        return _payment_required_from_empty(settings, repository)

    out_trade_no = result.get("out_trade_no") or ""
    order = repository.find_by_out_trade_no(out_trade_no) if out_trade_no else None
    if order is None:
        _debug("order_missing", {"has_out_trade_no": bool(out_trade_no)})
        return _payment_required_from_empty(settings, repository)

    # 沙箱验付响应可能不回传交易号、金额或资源，此时只按已绑定的本地订单取值。
    returned_trade_no = result.get("trade_no") or (trade_no if settings.is_exact_sandbox else "")
    verified_amount = result.get("amount") or (order["amount"] if settings.is_exact_sandbox else "")
    verified_resource = result.get("resource_id") or (
        order["resource_id"] if settings.is_exact_sandbox else ""
    )

    _debug(
        "verify_fields",
        {
            "active": result.get("active"),
            "trade_no_matches": returned_trade_no == trade_no,
            "amount": verified_amount,
            "has_resource": bool(verified_resource),
            "resource_matches": verified_resource == order["resource_id"],
            "stored_amount": order["amount"],
        },
    )
    if (
        result.get("active") is not True
        or not returned_trade_no
        or returned_trade_no != trade_no
        or not verified_resource
        or order["resource_id"] != RESOURCE_PATH
        or verified_resource != order["resource_id"]
        or not amounts_equal(order["amount"], verified_amount)
    ):
        return _payment_required_from_empty(settings, repository)

    query = json.loads(order.get("query_json") or "{}")

    def create_resource() -> str:
        return build_plan_content(query, RESOURCE_PATH)

    try:
        fulfillment = repository.prepare_fulfillment(
            {
                "out_trade_no": out_trade_no,
                "trade_no": returned_trade_no,
                "expected_amount": normalize_amount(verified_amount),
                "expected_resource_id": verified_resource,
                "create_resource": create_resource,
            }
        )
    except (QueryError, OrderError) as exc:
        _debug("prepare_rejected", {"error": type(exc).__name__, "message": str(exc)})
        return _payment_required_from_empty(settings, repository)

    service_result = fulfillment["service_result"]
    if fulfillment["state"] == "FULFILLED":
        return _success(returned_trade_no, out_trade_no, verified_resource, service_result, True)

    confirmed, reason = confirm_fulfillment(settings, returned_trade_no)
    _debug("confirm_result", {"confirmed": confirmed, "reason": reason})
    if not confirmed:
        return _json_response(
            {
                "code": "FULFILLMENT_CONFIRM_FAILED",
                "message": "资源已生成但履约确认失败，请用同一 Payment-Proof 重试",
                "detail": reason,
            },
            status=502,
        )

    repository.mark_fulfilled(out_trade_no, returned_trade_no)
    return _success(returned_trade_no, out_trade_no, verified_resource, service_result, False)


def _payment_required_from_empty(settings: AlipaySettings, repository: SqliteOrderRepository) -> Response:
    """验付未通过时重新出单，保证智能体可以继续支付。"""
    try:
        params = parse_query(_request_params())
    except QueryError:
        params = parse_query({})
    return _payment_required(params, settings, repository)


def _success(
    trade_no: str,
    out_trade_no: str,
    resource_id: str,
    service_result: str,
    already_fulfilled: bool,
) -> Response:
    validation = base64url_encode(
        json.dumps(
            {
                "trade_no": trade_no,
                "out_trade_no": out_trade_no,
                "validated": True,
                "resource_id": resource_id,
            },
            ensure_ascii=False,
        )
    )
    try:
        content = json.loads(service_result)
    except (TypeError, ValueError):
        content = service_result
    return _json_response(
        {
            "resource_id": resource_id,
            "content": content,
            "trade_no": trade_no,
            "out_trade_no": out_trade_no,
            "already_fulfilled": already_fulfilled,
            "fulfillment_confirmed": True,
        },
        headers={"Payment-Validation": validation},
    )


def main() -> None:
    try:
        app = create_app()
    except ConfigError as exc:
        raise SystemExit(f"启动失败：{exc}") from exc
    port = int(__import__("os").environ.get("MCD_SAVER_PORT", "5000"))
    app.run(host="127.0.0.1", port=port, debug=False)


if __name__ == "__main__":
    main()
