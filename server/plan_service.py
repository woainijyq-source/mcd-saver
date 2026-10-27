"""资源生成：调用项目既有引擎算出点单方案。

金额一律由 `mcd_saver` 引擎计算，服务端不做二次加总。
同一订单只生成一次，结果由订单仓库落库复用。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
DATA_DIR = SCRIPTS_DIR / "data"
RESOURCE_PATH = "/api/plan"
GOODS_NAME = "麦当劳省钱点单方案"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from mcd_saver import (  # noqa: E402
    Channel,
    Goal,
    SearchConfig,
    load_coupons,
    load_menu,
    recommend,
)


class QueryError(ValueError):
    """请求参数不合法。"""


def _optional_float(raw: Any, name: str, *, allow_zero: bool = False) -> float | None:
    if raw in (None, ""):
        return None
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise QueryError(f"{name} 必须是数字") from exc
    if value < 0 or (value == 0 and not allow_zero):
        raise QueryError(f"{name} 必须大于 0")
    return value


def _parse_time(raw: Any):
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise QueryError("time 格式应为 HH:MM")


def parse_query(params: Mapping[str, Any]) -> dict[str, Any]:
    channel_raw = str(params.get("channel") or "dine_in").strip().lower()
    try:
        channel = Channel(channel_raw)
    except ValueError as exc:
        raise QueryError("channel 只能是 dine_in / takeaway / delivery") from exc

    goal_raw = str(params.get("goal") or "").strip()
    goal = None
    if goal_raw:
        try:
            goal = Goal.parse(goal_raw)
        except ValueError as exc:
            raise QueryError("goal 取值不合法") from exc

    raw_must = params.get("must_include")
    if isinstance(raw_must, (list, tuple)):
        must_include = tuple(str(part).strip() for part in raw_must if str(part).strip())
    else:
        must_include_raw = str(raw_must or "").strip()
        must_include = tuple(part.strip() for part in must_include_raw.split(",") if part.strip())

    return {
        "budget": _optional_float(params.get("budget"), "budget"),
        "calories_max": _optional_float(params.get("calories_max"), "calories_max"),
        "channel": channel,
        "time": _parse_time(params.get("time")),
        "goal": goal,
        "must_include": must_include,
        "delivery_fee": _optional_float(params.get("delivery_fee"), "delivery_fee", allow_zero=True) or 0.0,
    }


def _offer_payload(offer) -> dict[str, Any]:
    plan = offer.plan
    return {
        "tag": offer.tag,
        "reason": offer.reason,
        "items": [
            {"sku": line.sku, "name": line.name, "qty": line.qty, "amount": line.amount}
            for line in plan.lines
        ],
        "coupon": plan.coupon.name if plan.coupon else None,
        "coupon_desc": plan.coupon.describe() if plan.coupon else None,
        "subtotal": plan.subtotal,
        "discount": plan.discount,
        "delivery_fee": plan.delivery_fee,
        "pay": plan.pay,
        "saved": plan.saved,
        "saving_ratio": round(plan.saving_ratio, 4),
        "calories": plan.calories,
        "calories_per_yuan": round(plan.calories_per_yuan, 2),
        "note": plan.note,
    }


def build_plan_content(params: Mapping[str, Any], resource_id: str = RESOURCE_PATH) -> str:
    query = parse_query(params)

    menu = load_menu(DATA_DIR / "sample_menu.json")
    coupons = load_coupons(DATA_DIR / "sample_coupons.json")

    config = SearchConfig(
        budget=query["budget"],
        max_calories=query["calories_max"],
        goal=query["goal"] or Goal.MIN_PAY,
        channel=query["channel"],
        now_time=query["time"],
        delivery_fee=query["delivery_fee"] if query["channel"] is Channel.DELIVERY else 0.0,
        must_include=query["must_include"],
    )
    offers = recommend(menu, coupons, config, now_time=query["time"])
    if not offers:
        raise QueryError("当前条件下没有可行点法，请放宽预算或热量限制")

    payload = {
        "status": "success",
        "service_type": "MCDONALD_DEAL_PLAN",
        "resource_id": resource_id,
        "goods_name": GOODS_NAME,
        "query": {
            "budget": query["budget"],
            "calories_max": query["calories_max"],
            "channel": query["channel"].value,
            "time": query["time"].strftime("%H:%M") if query["time"] else None,
            "goal": (query["goal"] or Goal.MIN_PAY).value,
            "must_include": list(query["must_include"]),
        },
        "plans": [_offer_payload(offer) for offer in offers],
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
    }
    return json.dumps(payload, ensure_ascii=False)
