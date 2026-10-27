"""优化引擎的回归测试。

算法类项目的通病是「能跑就算过」——没有测试，「单次最多选一样主食」这类
硬伤永远不会暴露。这里把关键行为全部钉死，改算法时跑一遍就知道有没有算错钱。

运行：
    python3 -m unittest discover -s tests -t . -v
或：
    python3 tests/test_optimizer.py
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcd_saver import (  # noqa: E402
    Channel,
    Coupon,
    CouponKind,
    Goal,
    Menu,
    MenuItem,
    SearchConfig,
    best_single,
    compute,
    detect_padding,
    load_coupons,
    load_menu,
    money,
    optimize,
    recommend,
)
from mcd_saver.coupons import QuoteContext, check  # noqa: E402

TODAY = date(2026, 10, 26)
NOON = time(12, 0)
MORNING = time(9, 0)


def small_menu() -> Menu:
    return Menu(
        [
            MenuItem("big_mac", "巨无霸", 25.0, 497, category="汉堡"),
            MenuItem("nuggets", "麦乐鸡10块", 26.0, 430, category="小食"),
            MenuItem("fries", "大薯条", 15.0, 460, category="小食"),
            MenuItem("hash", "薯饼", 4.0, 150, category="小食"),
            MenuItem("sauce", "甜酸酱", 0.5, 20, category="加购"),
            MenuItem("bf", "吉士蛋麦满分", 12.0, 300, category="早餐"),
        ]
    )


def ctx(t: time = NOON, channel: Channel = Channel.DINE_IN, fee: float = 0.0) -> QuoteContext:
    return QuoteContext(channel=channel, now_time=t, today=TODAY, delivery_fee=fee)


class TestCouponMath(unittest.TestCase):
    """券的计算必须精确到分，错一分钱就是信任问题。"""

    def setUp(self):
        self.menu = small_menu()

    def test_threshold_reached(self):
        c = Coupon("c1", "满30减5", CouponKind.THRESHOLD, threshold=30, discount=5)
        cut, _ = compute(c, {"nuggets": 1, "hash": 1}, self.menu, ctx())
        self.assertEqual(cut, 5.0)

    def test_threshold_not_reached(self):
        c = Coupon("c1", "满30减5", CouponKind.THRESHOLD, threshold=30, discount=5)
        cut, why = compute(c, {"big_mac": 1}, self.menu, ctx())
        self.assertEqual(cut, 0.0)
        self.assertIn("差 5.0 元", why)

    def test_rate_with_cap(self):
        c = Coupon("c2", "8折封顶6元", CouponKind.RATE, rate=0.8, max_cut=6.0)
        cut, _ = compute(c, {"nuggets": 2}, self.menu, ctx())  # 52 元, 20% = 10.4
        self.assertEqual(cut, 6.0)  # 被 6 元封顶

    def test_rate_without_cap(self):
        c = Coupon("c2", "8折", CouponKind.RATE, rate=0.8)
        cut, _ = compute(c, {"big_mac": 1}, self.menu, ctx())  # 25 * 0.2
        self.assertEqual(cut, 5.0)

    def test_bogo_enough(self):
        c = Coupon("c3", "买一送一", CouponKind.BOGO, target_sku="hash", buy_qty=1, free_qty=1)
        cut, _ = compute(c, {"hash": 2}, self.menu, ctx())
        self.assertEqual(cut, 4.0)

    def test_bogo_not_enough(self):
        c = Coupon("c3", "买一送一", CouponKind.BOGO, target_sku="hash", buy_qty=1, free_qty=1)
        cut, why = compute(c, {"hash": 1}, self.menu, ctx())
        self.assertEqual(cut, 0.0)
        self.assertIn("需买", why)

    def test_second_half(self):
        c = Coupon("c4", "第二份半价", CouponKind.BOGO, target_sku="fries", second_half=True)
        cut, _ = compute(c, {"fries": 2}, self.menu, ctx())
        self.assertEqual(cut, 7.5)  # 15 * 0.5

    def test_item_price(self):
        c = Coupon("c5", "巨无霸特价19.9", CouponKind.ITEM_PRICE, target_sku="big_mac", item_price=19.9)
        cut, _ = compute(c, {"big_mac": 1}, self.menu, ctx())
        self.assertEqual(cut, 5.1)

    def test_scope_limited_coupon_does_not_leak(self):
        """限定巨无霸的折扣券，不能拿去给薯条打折。"""
        c = Coupon(
            "c6", "巨无霸8折", CouponKind.RATE, rate=0.8, applies_to=("big_mac",)
        )
        cut, why = compute(c, {"fries": 2}, self.menu, ctx())
        self.assertEqual(cut, 0.0)
        self.assertIn("限指定商品", why)

    def test_scope_limited_coupon_uses_only_matched_amount(self):
        """巨无霸 25 + 薯条 15，8 折只能折巨无霸那 25 元。"""
        c = Coupon("c6", "巨无霸8折", CouponKind.RATE, rate=0.8, applies_to=("big_mac",))
        cut, _ = compute(c, {"big_mac": 1, "fries": 1}, self.menu, ctx())
        self.assertEqual(cut, 5.0)


class TestCouponAvailability(unittest.TestCase):
    def test_expired(self):
        c = Coupon("c", "过期券", CouponKind.THRESHOLD, expires_on=date(2026, 9, 30))
        verdict = check(c, ctx())
        self.assertFalse(verdict.ok)
        self.assertIn("过期", verdict.reason)

    def test_time_window_closed(self):
        c = Coupon("c", "早餐8折", CouponKind.RATE, rate=0.8, valid_to=time(10, 30))
        self.assertFalse(check(c, ctx(NOON)).ok)
        self.assertTrue(check(c, ctx(MORNING)).ok)

    def test_time_window_requires_known_time(self):
        """不知道现在几点，就不能让时段券生效。宁可不用，不能错用。"""
        c = Coupon("c", "早餐8折", CouponKind.RATE, rate=0.8, valid_to=time(10, 30))
        unknown = QuoteContext(channel=Channel.DINE_IN, now_time=None, today=TODAY)
        verdict = check(c, unknown)
        self.assertFalse(verdict.ok)
        self.assertIn("当前时间", verdict.reason)

    def test_channel_restricted(self):
        c = Coupon("c", "仅堂食", CouponKind.THRESHOLD, threshold=10, discount=2,
                   channels=(Channel.DINE_IN,))
        self.assertTrue(check(c, ctx(NOON, Channel.DINE_IN)).ok)
        self.assertFalse(check(c, ctx(NOON, Channel.DELIVERY)).ok)

    def test_delivery_only(self):
        c = Coupon("c", "免配送费", CouponKind.FREE_DELIVERY, threshold=30, delivery_only=True)
        self.assertFalse(check(c, ctx(NOON, Channel.DINE_IN)).ok)

    def test_one_coupon_per_order(self):
        """麦当劳一单只吃一张券，引擎不能给出叠加方案。"""
        menu = small_menu()
        coupons = [
            Coupon("a", "满20减3", CouponKind.THRESHOLD, threshold=20, discount=3),
            Coupon("b", "满25减4", CouponKind.THRESHOLD, threshold=25, discount=4),
        ]
        best, cut, _ = best_single(coupons, {"nuggets": 1}, menu, ctx(), only_one=True)
        self.assertEqual(best.coupon_id, "b")
        self.assertEqual(cut, 4.0)  # 不是 3+4=7


class TestOptimizer(unittest.TestCase):
    def setUp(self):
        self.menu = small_menu()

    def test_padding_actually_happens(self):
        """凑单的核心场景：26 元的麦乐鸡 + 4 元薯饼凑到 30，实付 25 比单点 26 更省。

        只遍历单品的算法做不到这件事——满减券永远用不上。
        """
        coupons = [Coupon("c", "满30减5", CouponKind.THRESHOLD, threshold=30, discount=5)]
        cfg = SearchConfig(
            budget=26,
            today=TODAY,
            now_time=NOON,
            goal=Goal.MIN_PAY,
            must_include=("nuggets",),
        )
        res = optimize(self.menu, coupons, cfg)
        self.assertTrue(res.offers, "凑单场景不应该没有结果")
        plan = res.offers[0].plan
        self.assertEqual(plan.pay, 25.0)
        self.assertIn("nuggets", plan.sku_qty())
        self.assertIn("hash", plan.sku_qty(), "应该自动加上薯饼凑门槛")

        note = detect_padding(plan, self.menu, coupons, cfg.quote_ctx(), cfg)
        self.assertIsNotNone(note)
        self.assertIn("凑单", note)

    def test_padding_does_not_overspend(self):
        """凑单不能让用户多花钱。凑了反而更贵，就该不凑。"""
        coupons = [Coupon("c", "满30减2", CouponKind.THRESHOLD, threshold=30, discount=2)]
        cfg = SearchConfig(
            budget=40,
            today=TODAY,
            now_time=NOON,
            goal=Goal.MIN_PAY,
            must_include=("nuggets",),
        )
        res = optimize(self.menu, coupons, cfg)
        self.assertTrue(res.offers)
        # 麦乐鸡 26 单点最省；凑到 30 减 2 = 28，反而更贵
        self.assertEqual(res.offers[0].plan.pay, 26.0)
        self.assertEqual(res.offers[0].plan.sku_qty(), {"nuggets": 1})

    def test_budget_is_hard_constraint(self):
        coupons = [Coupon("c", "满30减5", CouponKind.THRESHOLD, threshold=30, discount=5)]
        cfg = SearchConfig(budget=20, today=TODAY, now_time=NOON)
        res = optimize(self.menu, coupons, cfg)
        for offer in res.offers:
            self.assertLessEqual(offer.plan.pay, 20.0 + 1e-6)

    def test_calorie_cap(self):
        cfg = SearchConfig(max_calories=350, today=TODAY, now_time=NOON)
        res = optimize(self.menu, [], cfg)
        for offer in res.offers:
            self.assertLessEqual(offer.plan.calories, 350)

    def test_requires_main_dish(self):
        """不能推荐 0.4 元的甜酸酱。

        没有主食约束时，「实付最低」会退化到买最便宜的东西，
        数学上最优，业务上荒谬。
        """
        coupons = [Coupon("c", "8折", CouponKind.RATE, rate=0.8)]
        cfg = SearchConfig(budget=30, today=TODAY, now_time=NOON, goal=Goal.MIN_PAY)
        res = optimize(self.menu, coupons, cfg)
        self.assertTrue(res.offers)
        plan = res.offers[0].plan
        self.assertEqual(plan.sku_qty(), {"bf": 1}, "最便宜的主食是 12 元的早餐麦满分")

    def test_must_include_overrides_main_requirement(self):
        """用户说想吃麦乐鸡，就别硬塞汉堡给他。"""
        cfg = SearchConfig(
            budget=30, today=TODAY, now_time=NOON, must_include=("nuggets",), goal=Goal.MIN_PAY
        )
        res = optimize(self.menu, [], cfg)
        self.assertTrue(res.offers)
        self.assertIn("nuggets", res.offers[0].plan.sku_qty())

    def test_no_result_is_empty_not_crash(self):
        cfg = SearchConfig(budget=1, today=TODAY, now_time=NOON)
        res = optimize(self.menu, [], cfg)
        self.assertEqual(res.offers, [])

    def test_money_rounding(self):
        """0.1 + 0.2 那类浮点误差不能渗进金额。"""
        self.assertEqual(money(0.1 + 0.2), 0.3)
        self.assertEqual(money(19.9 + 0.1), 20.0)


class TestDeliveryFee(unittest.TestCase):
    """配送费只允许被减免一次。少收一笔配送费是财务事故。"""

    def setUp(self):
        self.menu = small_menu()
        self.coupon = Coupon(
            "f", "满30免配送", CouponKind.FREE_DELIVERY, threshold=30, delivery_only=True
        )

    def test_free_delivery_discount_equals_fee(self):
        cut, _ = compute(
            self.coupon, {"nuggets": 1, "hash": 1}, self.menu, ctx(NOON, Channel.DELIVERY, 9.0)
        )
        self.assertEqual(cut, 9.0)

    def test_free_delivery_not_double_counted(self):
        """30 元商品 + 9 元配送 - 9 元减免 = 实付 30，不能变成 21。"""
        from mcd_saver import build_plan, cart_lines

        cut, _ = compute(
            self.coupon, {"nuggets": 1, "hash": 1}, self.menu, ctx(NOON, Channel.DELIVERY, 9.0)
        )
        plan = build_plan(
            cart_lines(self.menu, {"nuggets": 1, "hash": 1}),
            self.coupon,
            cut,
            9.0,
            Channel.DELIVERY,
        )
        self.assertEqual(plan.pay, 30.0)
        self.assertEqual(plan.saved, 9.0)
        self.assertEqual(plan.original, 39.0)

    def test_delivery_fee_ignored_for_dine_in(self):
        cut, _ = compute(
            self.coupon, {"nuggets": 1, "hash": 1}, self.menu, ctx(NOON, Channel.DINE_IN, 9.0)
        )
        self.assertEqual(cut, 0.0)


class TestOnRealMenu(unittest.TestCase):
    """用完整演示菜单跑端到端，防止小样本测试掩盖问题。"""

    @classmethod
    def setUpClass(cls):
        root = Path(__file__).resolve().parent.parent
        cls.menu = load_menu(root / "data" / "sample_menu.json")
        cls.coupons = load_coupons(root / "data" / "sample_coupons.json")

    def test_recommend_returns_distinct_plans(self):
        cfg = SearchConfig(budget=30, today=TODAY, now_time=NOON)
        picks = recommend(self.menu, self.coupons, cfg)
        self.assertGreaterEqual(len(picks), 1)
        fingerprints = {tuple(sorted(o.plan.sku_qty().items())) for o in picks}
        self.assertEqual(len(fingerprints), len(picks), "推荐里不该有重复方案")

    def test_budget_respected_end_to_end(self):
        for budget in (15, 25, 40, 60):
            cfg = SearchConfig(budget=budget, today=TODAY, now_time=NOON)
            for offer in recommend(self.menu, self.coupons, cfg):
                self.assertLessEqual(
                    offer.plan.pay, budget + 1e-6, f"预算 {budget} 被突破：{offer.headline}"
                )

    def test_every_plan_has_main_dish(self):
        cfg = SearchConfig(budget=50, today=TODAY, now_time=NOON)
        for offer in recommend(self.menu, self.coupons, cfg):
            skus = offer.plan.sku_qty()
            self.assertTrue(
                any(self.menu[s].is_main for s in skus), f"{offer.headline} 没有主食"
            )

    def test_breakfast_coupon_only_in_morning(self):
        noon_cfg = SearchConfig(budget=25, today=TODAY, now_time=NOON)
        for offer in recommend(self.menu, self.coupons, noon_cfg):
            if offer.plan.coupon:
                self.assertNotEqual(
                    offer.plan.coupon.coupon_id, "rate_breakfast_80", "中午不该用早餐券"
                )

    def test_expired_coupon_never_used(self):
        cfg = SearchConfig(budget=60, today=TODAY, now_time=NOON)
        res = optimize(self.menu, self.coupons, cfg)
        used = {o.plan.coupon.coupon_id for o in res.offers if o.plan.coupon}
        self.assertNotIn("expired_25_4", used)

    def test_performance_is_sane(self):
        """120 项的真实菜单也要求秒出。这里给 3 秒的宽容上限。"""
        cfg = SearchConfig(budget=80, today=TODAY, now_time=NOON)
        res = optimize(self.menu, self.coupons, cfg)
        self.assertLess(res.elapsed_ms, 3000, f"太慢了：{res.elapsed_ms:.0f} ms")


class TestMcpAdapter(unittest.TestCase):
    """MCP 返回结构的适配。真实接入时这一层出问题，后面全错。"""

    def test_flat_list(self):
        from mcd_saver import menu_from_mcp

        payload = [
            {"code": "A1", "mealName": "巨无霸", "salePrice": "25.00", "categoryName": "汉堡"},
            {"code": "A2", "mealName": "大薯条", "salePrice": 15, "categoryName": "小食"},
        ]
        menu = menu_from_mcp(payload)
        self.assertEqual(len(menu), 2)
        self.assertEqual(menu["A1"].price, 25.0)
        self.assertTrue(menu["A1"].is_main, "汉堡应判定为主食")
        self.assertFalse(menu["A2"].is_main)

    def test_nested_categories(self):
        from mcd_saver import menu_from_mcp

        payload = {
            "code": 0,
            "data": {
                "categories": [
                    {"name": "套餐", "items": [
                        {"productCode": "S1", "productName": "板烧鸡腿堡套餐", "price": 37}
                    ]},
                    {"name": "饮料", "items": [
                        {"productCode": "D1", "productName": "中杯可乐", "price": 10}
                    ]},
                ]
            },
        }
        menu = menu_from_mcp(payload)
        self.assertEqual(len(menu), 2)
        self.assertTrue(menu["S1"].is_main)

    def test_missing_price_raises(self):
        """缺价格必须报错，不能当 0 元算。"""
        from mcd_saver import DataError, menu_from_mcp

        with self.assertRaises(DataError):
            menu_from_mcp([{"code": "X", "mealName": "神秘汉堡"}])

    def test_coupons_from_mcp(self):
        from mcd_saver import CouponKind, load_coupons_from_mcp

        payload = {
            "data": [
                {"couponId": "c1", "couponName": "满30减5", "minAmount": 30, "discountAmount": 5},
                {"couponId": "c2", "couponName": "8折", "discountRate": 0.8, "maxDiscount": 6},
                {"couponId": "c3", "couponName": "早餐券", "type": "threshold",
                 "threshold": 15, "discount": 2, "endTime": "10:30"},
            ]
        }
        coupons = load_coupons_from_mcp(payload)
        self.assertEqual(len(coupons), 3)
        self.assertIs(coupons[0].kind, CouponKind.THRESHOLD)
        self.assertIs(coupons[1].kind, CouponKind.RATE)
        self.assertEqual(coupons[1].max_cut, 6.0)
        self.assertEqual(coupons[2].valid_to, time(10, 30))

    def test_nutrition_nested(self):
        from mcd_saver import menu_from_mcp

        payload = [{
            "code": "N1", "mealName": "巨无霸", "price": 25,
            "nutrition": {"calories": 497, "protein": 25},
        }]
        menu = menu_from_mcp(payload)
        self.assertEqual(menu["N1"].calories, 497.0)
        self.assertEqual(menu["N1"].protein, 25.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
