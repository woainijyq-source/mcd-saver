"""优惠券的可用性判定与实际优惠计算。

优惠券规则最容易被写进提示词里交给模型自觉判断，但那等于把业务规则交给概率。
这里把规则写成代码，可单测、可复现、可解释。

判定顺序严格按现实逻辑：有效性 -> 渠道 -> 门店 -> 时段 -> 商品匹配 -> 门槛。
只要有一条不满足就返回具体原因，最终答复里能原样告诉用户「为什么这张券用不了」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, time
from typing import Mapping, Optional

from .models import Channel, Coupon, CouponKind, Menu, money


@dataclass(frozen=True, slots=True)
class QuoteContext:
    """结算上下文。真实场景由 now-time-info + 用户选择共同决定。"""

    channel: Channel = Channel.DINE_IN
    store_id: Optional[str] = None
    now_time: Optional[time] = None
    today: Optional[date] = None
    delivery_fee: float = 0.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    reason: str = ""


OK = CheckResult(True)


def _fmt_time(t: time) -> str:
    return t.strftime("%H:%M")


def check(coupon: Coupon, ctx: QuoteContext) -> CheckResult:
    """券在当前上下文中是否可用（不含门槛与商品量判定）。"""
    if coupon.expires_on is not None and ctx.today is not None:
        if ctx.today > coupon.expires_on:
            return CheckResult(False, f"已于 {coupon.expires_on:%Y-%m-%d} 过期")

    if coupon.channels and ctx.channel not in coupon.channels:
        allowed = "/".join(c.label for c in coupon.channels)
        return CheckResult(False, f"仅限{allowed}")

    if coupon.delivery_only and ctx.channel is not Channel.DELIVERY:
        return CheckResult(False, "仅限麦乐送")

    if coupon.store_ids and ctx.store_id and ctx.store_id not in coupon.store_ids:
        return CheckResult(False, "非当前门店可用")

    # 时段券：早餐卡这类，过了点就是废纸，必须硬拦
    has_window = bool(coupon.valid_from or coupon.valid_to)
    if has_window:
        if ctx.now_time is None:
            # 宁可不用券也不能错用券。不知道现在几点就让早餐券生效，
            # 用户到了 11 点发现下不了单，比不给推荐更糟。
            return CheckResult(False, "需要当前时间才能判断是否在适用时段")
        if coupon.valid_from and ctx.now_time < coupon.valid_from:
            return CheckResult(False, f"{_fmt_time(coupon.valid_from)} 后才可用")
        if coupon.valid_to and ctx.now_time > coupon.valid_to:
            return CheckResult(False, f"仅 {_fmt_time(coupon.valid_to)} 前可用")

    return OK


def _applicable_base(
    coupon: Coupon,
    cart: Mapping[str, int],
    menu: Menu,
    subtotal: float,
) -> tuple[float, list[str]]:
    """券能作用的金额，以及命中的商品名。

    门店折扣券绝大多数是「指定套餐 8 折」，如果按整单金额算折扣，
    就会出现「拿板烧套餐券去给薯条打折」这种下不了单的方案。
    """
    limited = bool(coupon.applies_to or coupon.applies_categories)
    if not limited:
        return subtotal, []

    total = 0.0
    names: list[str] = []
    for sku, qty in cart.items():
        if qty <= 0:
            continue
        item = menu[sku]
        hit = sku in coupon.applies_to
        if not hit and coupon.applies_categories:
            hit = any(cat in item.category for cat in coupon.applies_categories)
        if hit:
            total += item.price * qty
            names.append(item.name)
    return money(total), names


def compute(
    coupon: Coupon,
    cart: Mapping[str, int],
    menu: Menu,
    ctx: QuoteContext,
    subtotal: Optional[float] = None,
) -> tuple[float, str]:
    """计算这张券在给定购物车上能省多少。

    返回 (优惠金额, 说明)。金额为 0 表示这张券对当前购物车没有价值。

    `subtotal` 让调用方把已经算好的小计传进来。枚举阶段要跑几十万次，
    每次重新求和是纯浪费——这一项优化省掉了三分之一的耗时。
    """
    if subtotal is None:
        subtotal = money(sum(menu[sku].price * qty for sku, qty in cart.items() if qty > 0))
    if subtotal <= 0:
        return 0.0, "空购物车"

    kind = coupon.kind

    if kind is CouponKind.FREE_DELIVERY:
        fee = ctx.delivery_fee
        if ctx.channel is not Channel.DELIVERY or fee <= 0:
            return 0.0, "本单无配送费"
        if subtotal < coupon.threshold:
            return 0.0, f"差 {money(coupon.threshold - subtotal)} 元到免配门槛"
        return money(fee), f"免配送费 {fee:g} 元"

    if kind is CouponKind.THRESHOLD:
        base, names = _applicable_base(coupon, cart, menu, subtotal)
        if base <= 0:
            return 0.0, "限指定商品，本单没点"
        if base < coupon.threshold:
            return 0.0, f"差 {money(coupon.threshold - base)} 元到门槛"
        cut = money(min(coupon.discount, base))
        scope = f"（仅{names[0]}）" if names else ""
        return cut, f"满减 {coupon.discount:g} 元{scope}"

    if kind is CouponKind.RATE:
        base, names = _applicable_base(coupon, cart, menu, subtotal)
        if base <= 0:
            return 0.0, "限指定商品，本单没点"
        if base < coupon.threshold:
            return 0.0, f"差 {money(coupon.threshold - base)} 元到门槛"
        cut = base * (1 - float(coupon.rate))
        if not math.isinf(coupon.max_cut):
            cut = min(cut, coupon.max_cut)
        cut = money(min(cut, base))
        scope = f"（仅{names[0]}）" if names else ""
        return cut, f"折扣省 {cut:g} 元{scope}"

    if kind is CouponKind.ITEM_PRICE:
        sku = coupon.target_sku
        qty = cart.get(sku, 0) if sku else 0
        if not sku or qty <= 0:
            name = menu[sku].name if sku and menu.get(sku) else "指定商品"
            return 0.0, f"未点{name}"
        item = menu[sku]
        target = float(coupon.item_price or 0)
        if target >= item.price:
            return 0.0, "特价不低于原价"
        cut = money((item.price - target) * qty)
        return cut, f"{item.name}特价省 {cut:g} 元"

    if kind is CouponKind.BOGO:
        sku = coupon.target_sku
        qty = cart.get(sku, 0) if sku else 0
        if not sku or qty <= 0:
            return 0.0, "未点指定商品"
        item = menu[sku]
        if coupon.second_half:
            # 第二份半价：每 2 份省半份价
            pairs = qty // 2
            if pairs <= 0:
                return 0.0, f"需 2 份{item.name}"
            cut = money(item.price * 0.5 * pairs)
            return cut, f"第二份半价省 {cut:g} 元"
        group = coupon.buy_qty + coupon.free_qty
        if qty < group:
            return 0.0, f"需买 {coupon.buy_qty} 份才送 {coupon.free_qty} 份"
        times = qty // group
        cut = money(item.price * coupon.free_qty * times)
        return cut, f"买送省 {cut:g} 元"

    return 0.0, "未知券型"


def best_single(
    coupons: list[Coupon],
    cart: Mapping[str, int],
    menu: Menu,
    ctx: QuoteContext,
    only_one: bool = True,
    subtotal: Optional[float] = None,
) -> tuple[Optional[Coupon], float, list[tuple[Coupon, str]]]:
    """挑出这单最划算的券。

    麦当劳实际规则是一单只吃一张券（`only_one=True`）。以为券能叠加是常见误解，
    按叠加算会给出用户根本下不了的方案。

    返回 (最优券, 优惠额, 被排除券的原因列表)。
    """
    rejected: list[tuple[Coupon, str]] = []
    best: Optional[Coupon] = None
    best_cut = 0.0

    for coupon in coupons:
        verdict = check(coupon, ctx)
        if not verdict.ok:
            rejected.append((coupon, verdict.reason))
            continue
        cut, why = compute(coupon, cart, menu, ctx, subtotal)
        if cut <= 0:
            rejected.append((coupon, why))
            continue
        if cut > best_cut:
            best, best_cut = coupon, cut

    return best, money(best_cut), rejected


def coupon_combos(coupons: list[Coupon], max_stack: int = 1):
    """券组合迭代器。

    默认 max_stack=1（一单一张）。设 2 可评估"券 + 免配送费"这类真实叠加，
    但必须由调用方确认业务允许，不要默认开启。
    """
    usable = [c for c in coupons]
    n = len(usable)
    if max_stack <= 1:
        for c in usable:
            yield (c,)
        return
    from itertools import combinations

    for size in range(1, min(max_stack, n) + 1):
        for combo in combinations(usable, size):
            if size > 1 and not all(c.stackable for c in combo):
                continue
            yield combo


def combo_discount(
    combo: tuple[Coupon, ...],
    cart: Mapping[str, int],
    menu: Menu,
    ctx: QuoteContext,
) -> tuple[float, tuple[Coupon, ...]]:
    """一组券的合计优惠。单券直接返回。"""
    if len(combo) == 1:
        cut, _ = compute(combo[0], cart, menu, ctx)
        return cut, combo
    total = 0.0
    for c in combo:
        cut, _ = compute(c, cart, menu, ctx)
        total += cut
    return money(total), combo
