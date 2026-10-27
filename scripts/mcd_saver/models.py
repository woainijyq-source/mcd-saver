"""核心数据模型。

设计原则
--------
1. 与麦当劳 MCP 的返回结构解耦：`mcp_adapter` 负责把 MCP 的 JSON 翻译成本模型，
   算法层只认本模型。这样算法可以脱离网络单独测试。
2. 优惠券的所有约束（门槛 / 时段 / 渠道 / 门店 / 有效期 / 互斥）都建在模型上，
   而不是写在提示词里指望模型"记得住"。业务规则一旦交给自然语言去保证，
   就无法验证、无法复现，也没法回归测试。
3. 金额统一走 `money()` 收敛到分，避免浮点误差在多次加减后产生 0.01 级偏差。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, time
from enum import Enum
from typing import Iterable, Iterator, Mapping, Optional


def money(value: float) -> float:
    """把金额收敛到「分」。统一出口，避免到处 round 出不一致。"""
    return round(value + 1e-9, 2)


class Channel(str, Enum):
    """点餐渠道。券的适用范围与渠道强相关，必须显式建模。"""

    DINE_IN = "dine_in"
    TAKEAWAY = "takeaway"
    DELIVERY = "delivery"

    @property
    def label(self) -> str:
        return {
            Channel.DINE_IN: "堂食",
            Channel.TAKEAWAY: "外带",
            Channel.DELIVERY: "麦乐送",
        }[self]

    @classmethod
    def parse(cls, raw: str) -> "Channel":
        key = raw.strip().lower().replace("-", "_")
        alias = {
            "dine_in": cls.DINE_IN,
            "dinein": cls.DINE_IN,
            "堂食": cls.DINE_IN,
            "takeaway": cls.TAKEAWAY,
            "take_out": cls.TAKEAWAY,
            "外带": cls.TAKEAWAY,
            "self_pickup": cls.TAKEAWAY,
            "delivery": cls.DELIVERY,
            "麦乐送": cls.DELIVERY,
            "外送": cls.DELIVERY,
        }
        if key not in alias:
            raise ValueError(f"未知渠道: {raw}（可用：dine_in / takeaway / delivery）")
        return alias[key]


class CouponKind(str, Enum):
    """优惠券形态。覆盖麦当劳实际在售的券型。"""

    THRESHOLD = "threshold"      # 满 X 减 Y
    RATE = "rate"                # 折扣（可封顶）
    ITEM_PRICE = "item_price"    # 指定商品一口价
    BOGO = "bogo"                # 买一送一 / 第二份半价
    FREE_DELIVERY = "free_delivery"  # 免配送费

    @classmethod
    def parse(cls, raw: str) -> "CouponKind":
        key = raw.strip().lower().replace("-", "_")
        alias = {
            "threshold": cls.THRESHOLD,
            "full_reduction": cls.THRESHOLD,
            "满减": cls.THRESHOLD,
            "rate": cls.RATE,
            "discount": cls.RATE,
            "折扣": cls.RATE,
            "item_price": cls.ITEM_PRICE,
            "单品": cls.ITEM_PRICE,
            "bogo": cls.BOGO,
            "买一送一": cls.BOGO,
            "free_delivery": cls.FREE_DELIVERY,
            "免配送费": cls.FREE_DELIVERY,
        }
        if key not in alias:
            raise ValueError(f"未知券型: {raw}")
        return alias[key]


@dataclass(frozen=True, slots=True)
class MenuItem:
    """菜单上的一个可售单元（单品、套餐或加购品）。"""

    sku: str
    name: str
    price: float
    calories: float = 0.0
    protein: float = 0.0
    weight_g: float = 0.0
    category: str = ""
    is_combo: bool = False
    is_main: bool = False     # 是否算「一顿饭的主体」（汉堡/套餐/早餐），由 infer_is_main 判定
    max_qty: int = 2          # 单品在同一单里的合理上限，防枚举爆炸
    available: bool = True

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError(f"{self.sku} 价格不能为负")
        if self.max_qty < 1:
            object.__setattr__(self, "max_qty", 1)
        # 没显式指定就按品类/名字推断，保证直接构造 MenuItem 也能正确判定主食
        if not self.is_main:
            object.__setattr__(
                self,
                "is_main",
                infer_is_main(self.category, self.name, self.is_combo),
            )


MAIN_CATEGORY_HINTS = ("汉堡", "套餐", "早餐", "主餐", "主食", "卷", "堡")
MAIN_NAME_HINTS = ("堡", "满分", "卷", "套餐")


def infer_is_main(category: str, name: str, is_combo: bool = False) -> bool:
    """判断某餐品是否算「一顿饭的主体」。

    这个判定是必须的。否则「实付最低」会给出 0.4 元的甜酸酱、
    「每元热量最高」会给出大薯条配薯饼 —— 数学上都对，业务上都是错的。
    """
    if is_combo:
        return True
    if any(hint in category for hint in MAIN_CATEGORY_HINTS):
        return True
    return any(hint in name for hint in MAIN_NAME_HINTS)


@dataclass(slots=True)
class Coupon:
    """一张优惠券。

    约束字段默认「不限」，只有在明确填值时才会参与过滤，
    这样适配 MCP 返回的残缺数据时不会误杀可用券。
    """

    coupon_id: str
    name: str
    kind: CouponKind

    # 金额型
    threshold: float = 0.0            # 满 X 元可用（按商品小计算，不含配送费）
    discount: float = 0.0             # 满减金额
    rate: Optional[float] = None      # 0.8 表示 8 折
    max_cut: float = math.inf         # 折扣封顶

    # 单品型
    target_sku: Optional[str] = None
    item_price: Optional[float] = None
    buy_qty: int = 1                  # 买 N
    free_qty: int = 1                 # 送 M
    second_half: bool = False         # 第二份半价（BOGO 的变体）

    # 适用范围
    applies_to: tuple[str, ...] = ()          # SKU 白名单，如 ("combo_grilled_chicken",)
    applies_categories: tuple[str, ...] = ()  # 品类白名单，如 ("早餐",)
    channels: tuple[Channel, ...] = ()
    store_ids: tuple[str, ...] = ()
    delivery_only: bool = False

    # 时间约束
    valid_from: Optional[time] = None
    valid_to: Optional[time] = None
    expires_on: Optional[date] = None

    # 叠加
    stackable: bool = False           # 麦当劳实际上一单只吃一张券，默认 False

    source: str = ""                  # 券来源：麦麦省 / 门店 / 我的券

    def __post_init__(self) -> None:
        if self.kind is CouponKind.RATE:
            if self.rate is None:
                raise ValueError(f"折扣券 {self.coupon_id} 缺少 rate")
            if not 0 < self.rate < 1:
                raise ValueError(f"折扣券 {self.coupon_id} 的 rate 必须在 (0,1) 内")
        if self.kind in (CouponKind.ITEM_PRICE, CouponKind.BOGO) and not self.target_sku:
            raise ValueError(f"券 {self.coupon_id} 必须指定 target_sku")

    def describe(self) -> str:
        """人话描述，直接进最终答复，避免 LLM 二次编造。"""
        if self.kind is CouponKind.THRESHOLD:
            return f"满{self.threshold:g}减{self.discount:g}"
        if self.kind is CouponKind.RATE:
            cap = "" if math.isinf(self.max_cut) else f"封顶{self.max_cut:g}元"
            return f"{self.rate * 10:g}折{cap}"
        if self.kind is CouponKind.ITEM_PRICE:
            return f"指定商品{self.item_price:g}元"
        if self.kind is CouponKind.BOGO:
            if self.second_half:
                return "第二份半价"
            return f"买{self.buy_qty}送{self.free_qty}"
        return "免配送费"


@dataclass(frozen=True, slots=True)
class Line:
    """购物车里的一行。"""

    sku: str
    name: str
    unit_price: float
    qty: int
    calories: float = 0.0
    protein: float = 0.0
    weight_g: float = 0.0

    @property
    def amount(self) -> float:
        return money(self.unit_price * self.qty)

    @property
    def total_calories(self) -> float:
        return self.calories * self.qty


class Menu:
    """菜单索引。按 sku 取餐品，同时提供稳定遍历顺序（按价格降序）。"""

    def __init__(self, items: Iterable[MenuItem]):
        self._items: dict[str, MenuItem] = {}
        for item in items:
            if item.sku in self._items:
                raise ValueError(f"重复 sku: {item.sku}")
            self._items[item.sku] = item
        if not self._items:
            raise ValueError("菜单为空")

    def __getitem__(self, sku: str) -> MenuItem:
        return self._items[sku]

    def get(self, sku: str) -> Optional[MenuItem]:
        return self._items.get(sku)

    def __iter__(self) -> Iterator[MenuItem]:
        # 价格降序：DFS 剪枝依赖这个顺序
        return iter(sorted(self._items.values(), key=lambda m: (-m.price, m.sku)))

    def __len__(self) -> int:
        return len(self._items)

    @property
    def saleable(self) -> list[MenuItem]:
        return [m for m in self if m.available]

    @property
    def max_price(self) -> float:
        return max(m.price for m in self._items.values())


@dataclass
class Plan:
    """一个可执行的候选方案。"""

    lines: list[Line]
    coupon: Optional[Coupon]
    subtotal: float
    discount: float
    delivery_fee: float
    pay: float
    calories: float
    protein: float
    weight_g: float
    channel: Channel = Channel.DINE_IN
    note: str = ""

    @property
    def original(self) -> float:
        """优惠前的合计（含配送费），用于计算省钱比例。"""
        return money(self.subtotal + self.delivery_fee)

    @property
    def saved(self) -> float:
        return money(self.original - self.pay)

    @property
    def saving_ratio(self) -> float:
        return self.saved / self.original if self.original > 0 else 0.0

    @property
    def calories_per_yuan(self) -> float:
        return self.calories / self.pay if self.pay > 0 else 0.0

    @property
    def coupon_name(self) -> str:
        return self.coupon.name if self.coupon else "未用券"

    def sku_qty(self) -> dict[str, int]:
        return {ln.sku: ln.qty for ln in self.lines}


def build_plan(
    lines: list[Line],
    coupon: Optional[Coupon],
    discount: float,
    delivery_fee: float,
    channel: Channel = Channel.DINE_IN,
    note: str = "",
) -> Plan:
    """由行项目 + 已算好的优惠额，组装出 Plan。

    注意：免配送费券的 9 元已经由 coupons.compute 算进 discount 里了，
    这里必须保留原始配送费参与减法，不能既把 fee 清零又减一次 discount，
    否则实付会少收一笔配送费。
    """
    subtotal = money(sum(ln.amount for ln in lines))
    pay = money(max(subtotal + delivery_fee - discount, 0.0))
    return Plan(
        lines=lines,
        coupon=coupon,
        subtotal=subtotal,
        discount=money(discount),
        delivery_fee=money(delivery_fee),
        pay=pay,
        calories=sum(ln.total_calories for ln in lines),
        protein=sum(ln.protein * ln.qty for ln in lines),
        weight_g=sum(ln.weight_g * ln.qty for ln in lines),
        channel=channel,
        note=note,
    )


def cart_lines(menu: Menu, cart: Mapping[str, int]) -> list[Line]:
    """把 {sku: qty} 展开成行项目，顺序稳定。"""
    out: list[Line] = []
    for sku in sorted(cart, key=lambda s: (-menu[s].price, s)):
        qty = cart[sku]
        if qty <= 0:
            continue
        item = menu[sku]
        out.append(
            Line(
                sku=item.sku,
                name=item.name,
                unit_price=item.price,
                qty=qty,
                calories=item.calories,
                protein=item.protein,
                weight_g=item.weight_g,
            )
        )
    return out
