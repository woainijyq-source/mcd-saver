"""数据加载与 MCP 返回结构适配。

MCP 只负责「取数」，算法负责「算」。中间这一层把 MCP 的 JSON 翻译成内部模型。
字段名做了宽容匹配（多种候选名都试），因为 MCP 的返回结构会随版本演进，
硬编码单一字段名是脆的；但一旦缺关键字段就直接报错，不做静默兜底——
静默兜底最后一定会算错钱。
"""

from __future__ import annotations

import json
from datetime import date, time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .models import Channel, Coupon, CouponKind, Menu, MenuItem, infer_is_main


class DataError(ValueError):
    """数据不符合预期。宁可报错，不要算错。"""


def _first(payload: Mapping[str, Any], *keys: str, default=None):
    for key in keys:
        if key in payload and payload[key] not in (None, ""):
            return payload[key]
    return default


def _as_float(value: Any, field_name: str, default: Optional[float] = None) -> float:
    if value is None:
        if default is None:
            raise DataError(f"缺少字段: {field_name}")
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("¥", "").replace("元", "")
    try:
        return float(text)
    except ValueError as exc:
        raise DataError(f"字段 {field_name} 不是有效数字: {value!r}") from exc


def _parse_time(raw: Any) -> Optional[time]:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            from datetime import datetime

            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def _parse_date(raw: Any) -> Optional[date]:
    if raw in (None, ""):
        return None
    text = str(raw).strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def item_from_dict(payload: Mapping[str, Any]) -> MenuItem:
    """把一个 MCP 餐品对象转成 MenuItem。"""
    sku = _first(payload, "sku", "code", "productCode", "product_code", "mealCode", "id")
    if not sku:
        raise DataError(f"餐品缺少 sku/code: {payload}")
    name = _first(payload, "name", "mealName", "productName", "title", default=str(sku))

    price = _first(payload, "price", "salePrice", "currentPrice", "amount")
    if price is None:
        # 有些返回把价格放在 priceInfo 之类的子对象里
        for key in ("priceInfo", "pricing", "money"):
            sub = payload.get(key)
            if isinstance(sub, Mapping):
                price = _first(sub, "price", "salePrice", "currentPrice", "amount")
                if price is not None:
                    break
    if price is None:
        raise DataError(f"餐品 {sku} 缺少价格字段")

    nut = payload.get("nutrition") if isinstance(payload.get("nutrition"), Mapping) else {}
    category = str(_first(payload, "category", "categoryName", "type", default=""))
    is_combo = bool(_first(payload, "isCombo", "isSet", "combo", default=False)) or ("套餐" in category)
    raw_main = _first(payload, "isMain", "is_main")
    is_main = bool(raw_main) if raw_main is not None else infer_is_main(category, str(name), is_combo)

    return MenuItem(
        sku=str(sku),
        name=str(name),
        price=_as_float(price, f"{sku}.price"),
        calories=_as_float(
            _first(payload, "calories", "energy", "kcal") or _first(nut, "calories", "energy"),
            f"{sku}.calories",
            default=0.0,
        ),
        protein=_as_float(
            _first(payload, "protein") or _first(nut, "protein"), f"{sku}.protein", default=0.0
        ),
        weight_g=_as_float(
            _first(payload, "weightG", "weight", "netWeight"),
            f"{sku}.weight",
            default=0.0,
        ),
        category=category,
        is_combo=is_combo,
        is_main=is_main,
        max_qty=int(_first(payload, "maxQty", default=2) or 2),
        available=bool(_first(payload, "available", "isAvailable", "onSale", default=True)),
    )


def coupon_from_dict(payload: Mapping[str, Any]) -> Coupon:
    """把一张券的 JSON 转成 Coupon。券型字段缺失时按金额特征推断。"""
    cid = _first(payload, "couponId", "coupon_id", "id", "code")
    if not cid:
        raise DataError(f"券缺少 id: {payload}")
    name = str(_first(payload, "name", "couponName", "title", default=str(cid)))

    raw_kind = _first(payload, "kind", "type", "couponType", "coupon_type")
    if raw_kind:
        kind = CouponKind.parse(str(raw_kind))
    elif _first(payload, "rate", "discountRate") is not None:
        kind = CouponKind.RATE
    elif _first(payload, "targetSku", "sku") is not None:
        kind = CouponKind.BOGO if _first(payload, "buyQty") else CouponKind.ITEM_PRICE
    else:
        kind = CouponKind.THRESHOLD

    channels: tuple[Channel, ...] = ()
    raw_channels = _first(payload, "channels", "channel", "applyChannel")
    if raw_channels:
        if isinstance(raw_channels, str):
            raw_channels = [raw_channels]
        channels = tuple(Channel.parse(str(c)) for c in raw_channels)

    store_ids: tuple[str, ...] = ()
    raw_stores = _first(payload, "storeIds", "stores", "storeId")
    if raw_stores:
        if isinstance(raw_stores, str):
            raw_stores = [raw_stores]
        store_ids = tuple(str(s) for s in raw_stores)

    applies_to: tuple[str, ...] = ()
    raw_apply = _first(payload, "appliesTo", "applicableSkus", "skuList")
    if raw_apply:
        if isinstance(raw_apply, str):
            raw_apply = [raw_apply]
        applies_to = tuple(str(s) for s in raw_apply)

    applies_categories: tuple[str, ...] = ()
    raw_cats = _first(payload, "appliesCategories", "applicableCategories", "categoryList")
    if raw_cats:
        if isinstance(raw_cats, str):
            raw_cats = [raw_cats]
        applies_categories = tuple(str(s) for s in raw_cats)

    return Coupon(
        coupon_id=str(cid),
        name=name,
        kind=kind,
        threshold=_as_float(_first(payload, "threshold", "minAmount", "condition"), "threshold", 0.0),
        discount=_as_float(_first(payload, "discount", "discountAmount", "reduce"), "discount", 0.0),
        rate=(
            _as_float(_first(payload, "rate", "discountRate"), "rate", None)
            if _first(payload, "rate", "discountRate") is not None
            else None
        ),
        max_cut=_as_float(
            _first(payload, "maxCut", "maxDiscount", "cap"), "max_cut", float("inf")
        ),
        target_sku=(str(_first(payload, "targetSku", "sku")) if _first(payload, "targetSku", "sku") else None),
        item_price=(
            _as_float(_first(payload, "itemPrice"), "item_price", None)
            if _first(payload, "itemPrice") is not None
            else None
        ),
        buy_qty=int(_first(payload, "buyQty", default=1) or 1),
        free_qty=int(_first(payload, "freeQty", default=1) or 1),
        second_half=bool(_first(payload, "secondHalf", default=False)),
        applies_to=applies_to,
        applies_categories=applies_categories,
        channels=channels,
        store_ids=store_ids,
        delivery_only=bool(_first(payload, "deliveryOnly", default=False)),
        valid_from=_parse_time(_first(payload, "validFrom", "startTime")),
        valid_to=_parse_time(_first(payload, "validTo", "endTime")),
        expires_on=_parse_date(_first(payload, "expiresOn", "expireDate", "endDate")),
        stackable=bool(_first(payload, "stackable", default=False)),
        source=str(_first(payload, "source", default="")),
    )


def menu_from_mcp(payload: Any) -> Menu:
    """从 query-meals 一类返回里抽出菜单。

    兼容三种常见包裹形态：裸数组 / {"data": [...]} / {"categories":[{"items":[...]}]}。
    """
    rows: list[Mapping[str, Any]] = []

    def collect(node: Any) -> None:
        if isinstance(node, list):
            for sub in node:
                collect(sub)
        elif isinstance(node, Mapping):
            if any(k in node for k in ("categories", "data", "items", "meals", "products")):
                for key in ("categories", "data", "items", "meals", "products"):
                    if key in node:
                        collect(node[key])
            elif any(k in node for k in ("sku", "code", "productCode", "mealCode", "id")):
                rows.append(node)

    collect(payload)

    if not rows:
        raise DataError("未能从返回中识别出餐品列表，请检查字段映射")

    return Menu(item_from_dict(row) for row in rows)


def load_menu(path: str | Path) -> Menu:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_items = data.get("items", data) if isinstance(data, Mapping) else data
    return Menu(item_from_dict(row) for row in raw_items)


def load_coupons(path: str | Path) -> list[Coupon]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("coupons", data) if isinstance(data, Mapping) else data
    return [coupon_from_dict(row) for row in raw]


def load_coupons_from_mcp(payload: Any) -> list[Coupon]:
    """从 query-my-coupons / available-coupons 的返回里抽券列表。"""
    rows: list[Mapping[str, Any]] = []

    def collect(node: Any) -> None:
        if isinstance(node, list):
            for sub in node:
                collect(sub)
        elif isinstance(node, Mapping):
            if any(k in node for k in ("coupons", "data", "items", "list")):
                for key in ("coupons", "data", "items", "list"):
                    if key in node:
                        collect(node[key])
            elif any(k in node for k in ("couponId", "coupon_id", "id")):
                rows.append(node)

    collect(payload)
    return [coupon_from_dict(row) for row in rows]


def parse_coupons_arg(raw: Iterable[str]) -> list[Coupon]:
    """支持命令行直接传券，如 --coupon "满30减5:30:5"。"""
    out: list[Coupon] = []
    for idx, text in enumerate(raw):
        parts = [p.strip() for p in text.split(":")]
        if len(parts) < 3:
            raise DataError(f'券格式应为 "名称:门槛:优惠额"，收到 {text!r}')
        out.append(
            Coupon(
                coupon_id=f"cli-{idx}",
                name=parts[0],
                kind=CouponKind.THRESHOLD,
                threshold=float(parts[1]),
                discount=float(parts[2]),
            )
        )
    return out
