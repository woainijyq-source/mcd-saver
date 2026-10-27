"""组合优化器：真正解决「凑单」这件事。

只遍历单品的话满减券永远用不上——而满减券的全部价值就在凑单。
这里用「锚点 + 补充品组合」的枚举方式，
并在枚举阶段就算好小计，只在通过约束后才构造 Plan，保证 120 个 SKU 的
真实菜单也能在 1 秒内出结果。

另一个重要区别：不再输出一个"最优解"，而是输出多个维度的方案。
"最便宜"和"最划算"和"最能吃饱"本来就不是同一件事，硬压成一个答案
必然误导用户。
"""

from __future__ import annotations

import itertools
import time as _time
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Iterator, Optional

from .coupons import QuoteContext, best_single, check, compute
from .models import (
    Channel,
    Coupon,
    CouponKind,
    Menu,
    MenuItem,
    Plan,
    build_plan,
    cart_lines,
    money,
)


class Goal(str, Enum):
    """优化目标。真实点餐场景下这几个目标会给出不同答案，必须让用户选。"""

    MIN_PAY = "min_pay"            # 实付最低（满足约束的前提下）
    MAX_SAVING = "max_saving"      # 省的钱最多
    BEST_VALUE = "best_value"      # 每元热量最高（吃饱性价比）
    MAX_RATIO = "max_ratio"        # 省钱比例最高（容易推荐最便宜的东西，慎用）

    @classmethod
    def parse(cls, raw: str) -> "Goal":
        key = raw.strip().lower().replace("-", "_")
        alias = {
            "min_pay": cls.MIN_PAY, "cheapest": cls.MIN_PAY,
            "最便宜": cls.MIN_PAY, "实付最低": cls.MIN_PAY,
            "max_saving": cls.MAX_SAVING, "省最多": cls.MAX_SAVING,
            "best_value": cls.BEST_VALUE, "性价比": cls.BEST_VALUE,
            "吃饱": cls.BEST_VALUE, "最能吃饱": cls.BEST_VALUE,
            "max_ratio": cls.MAX_RATIO, "比例": cls.MAX_RATIO,
        }
        if key not in alias:
            raise ValueError(f"未知目标: {raw}")
        return alias[key]

    @property
    def label(self) -> str:
        return {
            Goal.MIN_PAY: "实付最低",
            Goal.MAX_SAVING: "省钱最多",
            Goal.BEST_VALUE: "每元最扛饿",
            Goal.MAX_RATIO: "省钱比例最高",
        }[self]


@dataclass(slots=True)
class SearchConfig:
    """一次搜索的输入条件。所有约束都是硬约束，不满足直接剔除。"""

    budget: Optional[float] = None          # 预算，指「实付」而非小计
    max_calories: Optional[float] = None
    min_calories: Optional[float] = None    # "想吃饱"用这个，别用预算硬顶
    goal: Goal = Goal.MIN_PAY
    channel: Channel = Channel.DINE_IN
    store_id: Optional[str] = None
    now_time: Optional[_time.time] = None
    today: Optional[date] = None
    delivery_fee: float = 0.0
    max_addons: int = 2                     # 一个主体最多配几件补充品
    addon_pool_size: int = 28               # 参与凑单的低价候选数量
    top_k: int = 6
    max_total: Optional[float] = None       # 小计上界，默认按券的最大优惠能力推算
    only_one_coupon: bool = True
    require_main: bool = True               # 必须含一份主食，否则会推荐 0.4 元的甜酸酱
    must_include: tuple[str, ...] = ()      # 必须包含的 SKU，对应「我就想吃这个，怎么最省」

    def quote_ctx(self, now_time=None) -> QuoteContext:
        return QuoteContext(
            channel=self.channel,
            store_id=self.store_id,
            now_time=now_time if now_time is not None else self.now_time,
            today=self.today,
            delivery_fee=self.delivery_fee,
        )

    def __post_init__(self) -> None:
        if self.max_addons < 0:
            self.max_addons = 0
        if self.budget is not None and self.budget <= 0:
            raise ValueError("预算必须大于 0")


@dataclass(slots=True)
class Offer:
    """一条推荐结果，带人话解释。"""

    plan: Plan
    tag: str = ""
    reason: str = ""

    @property
    def headline(self) -> str:
        items = " + ".join(f"{ln.name}x{ln.qty}" if ln.qty > 1 else ln.name for ln in self.plan.lines)
        return items


@dataclass(slots=True)
class Result:
    offers: list[Offer] = field(default_factory=list)
    rejected: list[tuple[Coupon, str]] = field(default_factory=list)
    scanned: int = 0
    elapsed_ms: float = 0.0

    @property
    def best(self) -> Optional[Offer]:
        return self.offers[0] if self.offers else None


def _score(plan: Plan, goal: Goal) -> float:
    """统一转成「越大越好」。"""
    if goal is Goal.MIN_PAY:
        return -plan.pay
    if goal is Goal.MAX_SAVING:
        return plan.saved
    if goal is Goal.BEST_VALUE:
        return plan.calories_per_yuan
    return plan.saving_ratio


def _max_cut_capacity(coupons: list[Coupon]) -> float:
    """所有券里单张最多能省多少。用来推小计上界，避免枚举无意义的高价组合。"""
    cap = 0.0
    for c in coupons:
        if c.kind is CouponKind.THRESHOLD:
            cap = max(cap, c.discount)
        elif c.kind is CouponKind.RATE and c.rate is not None:
            bound = c.max_cut if c.max_cut != float("inf") else 20.0
            cap = max(cap, bound)
        elif c.kind is CouponKind.FREE_DELIVERY:
            cap = max(cap, 15.0)
    return cap


def _iter_carts(
    items: list[MenuItem],
    limit: float,
    max_addons: int,
    addon_pool_size: int,
) -> Iterator[tuple[dict[str, int], float, float]]:
    """枚举候选购物车。

    产出 (cart, subtotal, calories)。三种形态：
      1. 单品单点（含 2 份，用于买一送一 / 第二份半价）
      2. 主体 + 1~max_addons 件低价补充品（这是凑单能力的来源）
      3. 纯低价组合（预算极低时也能有答案）
    """
    cheap_sorted = sorted(items, key=lambda m: (m.price, m.sku))
    pool = cheap_sorted[: max(0, addon_pool_size)]

    # 预先算好补充品组合，避免在主体循环里重复组合
    addon_sets: list[tuple[tuple[MenuItem, ...], float, float]] = []
    for size in range(1, max_addons + 1):
        for combo in itertools.combinations(pool, size):
            price = sum(m.price for m in combo)
            if price > limit:
                continue
            addon_sets.append((combo, price, sum(m.calories for m in combo)))

    seen: set[tuple[tuple[str, int], ...]] = set()

    def emit(cart: dict[str, int], price: float, cal: float):
        key = tuple(sorted(cart.items()))
        if key in seen:
            return None
        seen.add(key)
        return cart, price, cal

    # 1. 单品（可多份）
    for item in items:
        if item.price > limit:
            continue
        max_q = min(item.max_qty, int(limit // item.price))
        for q in range(1, max_q + 1):
            out = emit({item.sku: q}, item.price * q, item.calories * q)
            if out:
                yield out

    # 2. 主体 + 补充品
    # 注意：这里不再单独生成「纯补充品组合」——当主体恰好是最便宜那件时，
    # 第 2 步已经覆盖了它，重复生成只是白白多做一轮去重。
    if max_addons > 0:
        for anchor in items:
            if anchor.price > limit:
                continue
            for combo, price, cal in addon_sets:
                if any(m.sku == anchor.sku for m in combo):
                    continue
                total = anchor.price + price
                if total > limit:
                    continue
                cart = {anchor.sku: 1}
                for m in combo:
                    cart[m.sku] = cart.get(m.sku, 0) + 1
                if any(v > 2 for v in cart.values()):
                    continue
                out = emit(cart, total, anchor.calories + cal)
                if out:
                    yield out


def _passes(plan: Plan, cfg: SearchConfig) -> bool:
    if cfg.budget is not None and plan.pay > cfg.budget + 1e-6:
        return False
    if cfg.max_calories is not None and plan.calories > cfg.max_calories:
        return False
    if cfg.min_calories is not None and plan.calories < cfg.min_calories:
        return False
    return True


def detect_padding(
    plan: Plan,
    menu: Menu,
    coupons: list[Coupon],
    ctx: QuoteContext,
    cfg: SearchConfig,
) -> Optional[str]:
    """检测「凑单跳板」：某一行存在的唯一意义是把小计顶过券门槛。

    判据是去掉这一行后，重新选券，实付反而更高。这是真省钱的核心动作，
    也是最该主动告诉用户的一句话。
    """
    if len(plan.lines) < 2:
        return None
    cheapest = min(plan.lines, key=lambda ln: (ln.unit_price, ln.sku))
    cart = plan.sku_qty()
    cart[cheapest.sku] -= 1
    if cart[cheapest.sku] <= 0:
        cart.pop(cheapest.sku)

    subtotal2 = money(sum(menu[s].price * q for s, q in cart.items()))
    _, cut2, _ = best_single(
        coupons, cart, menu, ctx, only_one=cfg.only_one_coupon, subtotal=subtotal2
    )
    pay2 = money(max(subtotal2 + plan.delivery_fee - cut2, 0.0))

    if pay2 > plan.pay + 0.005:
        return (
            f"加 {cheapest.name}（{cheapest.unit_price:g} 元）凑单，"
            f"实付反而少 {money(pay2 - plan.pay)} 元"
        )
    return None


def optimize(
    menu: Menu,
    coupons: list[Coupon],
    cfg: SearchConfig,
    now_time=None,
) -> Result:
    """主入口。返回多个维度的推荐方案。"""
    started = _time.perf_counter()
    ctx = cfg.quote_ctx(now_time)

    items = [m for m in menu.saleable]
    if not items:
        return Result()

    # 小计上界：预算 + 单张券的最大优惠能力。再高只会多花钱不多省钱。
    limit = cfg.max_total
    if limit is None:
        if cfg.budget is not None:
            limit = cfg.budget + _max_cut_capacity(coupons) + 1.0
        else:
            thresholds = [c.threshold for c in coupons if c.threshold > 0]
            limit = (max(thresholds) * 1.15 if thresholds else 60.0) + 1.0

    # 时段券提前过滤，省得在枚举里反复判断
    usable: list[Coupon] = []
    rejected: list[tuple[Coupon, str]] = []
    for c in coupons:
        verdict = check(c, ctx)
        if verdict.ok:
            usable.append(c)
        else:
            rejected.append((c, verdict.reason))

    scored: list[tuple[float, Plan]] = []
    scanned = 0

    # 菜单里压根没有主食时就自动放宽，不然一条结果都出不来。
    # 用户明确指定要吃什么（must_include）时也不再强制主食：
    # 他说「想吃麦乐鸡」，这时候塞个汉堡给他才是错的。
    require_main = (
        cfg.require_main
        and not cfg.must_include
        and any(m.is_main for m in items)
    )

    for cart, subtotal, calories in _iter_carts(items, limit, cfg.max_addons, cfg.addon_pool_size):
        scanned += 1
        if require_main and not any(menu[sku].is_main for sku in cart):
            continue
        if cfg.must_include and not all(sku in cart for sku in cfg.must_include):
            continue
        if cfg.max_calories is not None and calories > cfg.max_calories:
            continue
        if cfg.min_calories is not None and calories < cfg.min_calories:
            continue

        coupon, cut, _ = best_single(
            usable, cart, menu, ctx, only_one=cfg.only_one_coupon, subtotal=subtotal
        )

        fee = cfg.delivery_fee if cfg.channel is Channel.DELIVERY else 0.0
        plan = build_plan(cart_lines(menu, cart), coupon, cut, fee, cfg.channel)
        if not _passes(plan, cfg):
            continue
        scored.append((_score(plan, cfg.goal), plan))

    elapsed_ms = (_time.perf_counter() - started) * 1000

    if not scored:
        return Result(offers=[], rejected=rejected, scanned=scanned, elapsed_ms=elapsed_ms)

    # 同券同菜品只留一条，避免一模一样的东西占满列表
    scored.sort(key=lambda pair: (-pair[0], pair[1].pay))
    offers: list[Offer] = []
    fingerprints: set[tuple] = set()
    for _, plan in scored:
        fp = (tuple(sorted(plan.sku_qty().items())), plan.coupon.coupon_id if plan.coupon else "")
        if fp in fingerprints:
            continue
        fingerprints.add(fp)
        note = detect_padding(plan, menu, coupons, ctx, cfg)
        offers.append(
            Offer(
                plan=plan,
                tag=cfg.goal.label,
                reason=note or "",
            )
        )
        if len(offers) >= cfg.top_k:
            break

    return Result(offers=offers, rejected=rejected, scanned=scanned, elapsed_ms=elapsed_ms)


def recommend(
    menu: Menu,
    coupons: list[Coupon],
    cfg: SearchConfig,
    now_time=None,
) -> list[Offer]:
    """给用户看的多维推荐，去重后返回。

    为什么默认维度里没有「省钱最多」和「省钱比例最高」：
    这两个目标会系统性推荐「买更贵的东西省更多」。实测中「比例最高」会把
    24 元的方案换成 25 元的方案，只因为后者折扣率 16.7% 而前者 11.1% ——
    用户多花 1 元买了一个更好看的折扣数字。这类建议在业务上是误导，
    保留给 `--goal` 显式调用，但不进默认推荐。

    默认维度：
      1. 每元最扛饿 —— 单位货币换到的热量最多，这才是「划算」的本义
      2. 实付最低   —— 预算敏感时的底价解（已强制包含主食）
      3. 加几块升级 —— 增量视角：再多花一点能多吃多少，回答「要不要加个鸡翅」
    """
    picks: list[Offer] = []
    used: set[tuple] = set()

    def _clone(goal: Goal, top_k: int = 1, budget: Optional[float] = None) -> SearchConfig:
        return SearchConfig(
            budget=budget if budget is not None else cfg.budget,
            max_calories=cfg.max_calories,
            min_calories=cfg.min_calories,
            goal=goal,
            channel=cfg.channel,
            store_id=cfg.store_id,
            now_time=cfg.now_time,
            today=cfg.today,
            delivery_fee=cfg.delivery_fee,
            max_addons=cfg.max_addons,
            addon_pool_size=cfg.addon_pool_size,
            top_k=top_k,
            max_total=cfg.max_total,
            only_one_coupon=cfg.only_one_coupon,
            require_main=cfg.require_main,
            must_include=cfg.must_include,
        )

    base: Optional[Offer] = None
    for goal in (Goal.BEST_VALUE, Goal.MIN_PAY):
        res = optimize(menu, coupons, _clone(goal), now_time)
        if not res.offers:
            continue
        offer = res.offers[0]
        if base is None:
            base = offer
        fp = tuple(sorted(offer.plan.sku_qty().items()))
        if fp in used:
            continue
        used.add(fp)
        offer.tag = goal.label
        picks.append(offer)

    # 增量建议：在首选方案基础上多花不超过 8 元，能多吃多少
    if base is not None:
        headroom = cfg.budget - base.plan.pay if cfg.budget else 8.0
        headroom = min(headroom, 8.0)
        if headroom >= 3.0:
            up_cfg = _clone(Goal.BEST_VALUE, budget=base.plan.pay + headroom)
            up_cfg.min_calories = base.plan.calories + 150
            up = optimize(menu, coupons, up_cfg, now_time)
            if up.offers:
                cand = up.offers[0]
                fp = tuple(sorted(cand.plan.sku_qty().items()))
                extra_pay = money(cand.plan.pay - base.plan.pay)
                extra_cal = cand.plan.calories - base.plan.calories
                if fp not in used and extra_pay >= 0.5 and extra_cal > 80:
                    cand.tag = "加几块升级"
                    cand.reason = (
                        f"比上一档多花 ¥{extra_pay:.2f}，多吃 {extra_cal:.0f} 大卡"
                        f"（每元多 {extra_cal / extra_pay:.0f} 大卡）"
                    )
                    used.add(fp)
                    picks.append(cand)

    return picks
