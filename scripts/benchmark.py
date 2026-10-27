#!/usr/bin/env python3
"""性能基准：验证不同菜单规模下的搜索耗时。

枚举是组合爆炸的重灾区，所以「能跑」和「能秒出」是两件事。
真实单店菜单大概 80~150 项，这里从 40 项压到 400 项，看拐点在哪。

用法：
    python3 scripts/benchmark.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import date  # noqa: E402

from mcd_saver import Coupon, CouponKind, Menu, MenuItem, SearchConfig, optimize  # noqa: E402

TODAY = date(2026, 10, 26)


def synthetic_menu(size: int) -> Menu:
    """造一份规模可控的菜单：价格 3~45 元，热量随价格走。"""
    items = []
    for i in range(size):
        price = 3.0 + (i * 42.0 / max(size - 1, 1))
        items.append(
            MenuItem(
                sku=f"sku_{i:04d}",
                name=f"餐品{i}",
                price=round(price, 1),
                calories=round(price * 18),
                category="汉堡" if i % 6 == 0 else "小食",
                is_combo=(i % 12 == 0),
                is_main=(i % 6 == 0) or (i % 12 == 0),
                max_qty=2,
            )
        )
    return Menu(items)


def synthetic_coupons() -> list[Coupon]:
    return [
        Coupon("b20", "满20减3", CouponKind.THRESHOLD, threshold=20, discount=3),
        Coupon("b30", "满30减5", CouponKind.THRESHOLD, threshold=30, discount=5),
        Coupon("b50", "满50减10", CouponKind.THRESHOLD, threshold=50, discount=10),
        Coupon("b80", "满80减18", CouponKind.THRESHOLD, threshold=80, discount=18),
        Coupon("r80", "8折封顶10", CouponKind.RATE, rate=0.8, max_cut=10),
    ]


def main() -> int:
    print(f"{'菜单项数':>8} {'候选数':>10} {'耗时(ms)':>10} {'结果数':>8}")
    print("-" * 42)
    slowest = 0.0
    for size in (40, 80, 120, 200, 400):
        menu = synthetic_menu(size)
        coupons = synthetic_coupons()
        cfg = SearchConfig(budget=60, today=TODAY, max_addons=2)
        started = time.perf_counter()
        res = optimize(menu, coupons, cfg)
        elapsed = (time.perf_counter() - started) * 1000
        slowest = max(slowest, elapsed)
        print(f"{size:>8} {res.scanned:>10} {elapsed:>10.1f} {len(res.offers):>8}")

    print("-" * 42)
    verdict = "可接受" if slowest < 1000 else "偏慢，需要优化 addon_pool_size"
    print(f"最慢一次 {slowest:.0f} ms —— {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
