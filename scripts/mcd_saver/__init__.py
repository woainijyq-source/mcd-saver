"""麦麦省钱助手 —— 可验证的麦当劳点餐省钱引擎。

公开 API::

    from mcd_saver import load_menu, load_coupons, SearchConfig, optimize, recommend

    menu = load_menu("data/sample_menu.json")
    coupons = load_coupons("data/sample_coupons.json")
    cfg = SearchConfig(budget=30, goal=Goal.MIN_PAY)
    picks = recommend(menu, coupons, cfg)
"""

from __future__ import annotations

__version__ = "2.0.0"

from .coupons import CheckResult, QuoteContext, best_single, check, compute
from .loader import (
    DataError,
    coupon_from_dict,
    item_from_dict,
    load_coupons,
    load_coupons_from_mcp,
    load_menu,
    menu_from_mcp,
)
from .models import (
    Channel,
    Coupon,
    CouponKind,
    Line,
    Menu,
    MenuItem,
    Plan,
    build_plan,
    cart_lines,
    infer_is_main,
    money,
)
from .optimizer import Goal, Offer, Result, SearchConfig, detect_padding, optimize, recommend

__all__ = [
    "__version__",
    "Channel",
    "Coupon",
    "CouponKind",
    "Line",
    "Menu",
    "MenuItem",
    "Plan",
    "Goal",
    "Offer",
    "Result",
    "SearchConfig",
    "QuoteContext",
    "CheckResult",
    "best_single",
    "check",
    "compute",
    "detect_padding",
    "optimize",
    "recommend",
    "build_plan",
    "cart_lines",
    "infer_is_main",
    "money",
    "load_menu",
    "load_coupons",
    "load_coupons_from_mcp",
    "menu_from_mcp",
    "coupon_from_dict",
    "item_from_dict",
    "DataError",
]
