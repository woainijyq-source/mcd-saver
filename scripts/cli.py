#!/usr/bin/env python3
"""麦麦省钱助手 —— 命令行入口。

不接 MCP 也能跑：自带演示菜单和券包。
接了 MCP 就把 query-meals / query-my-coupons 的返回喂给 loader，再调同一套算法。

用法示例
--------
    python cli.py                                  # 跑内置三个场景
    python cli.py --budget 30                      # 30 元内怎么点
    python cli.py --budget 30 --calories-max 600   # 30 元内、600 卡以内
    python cli.py --budget 40 --goal best_value    # 40 元内最扛饿
    python cli.py --time 09:15 --budget 25         # 早上 9 点，早餐券生效
    python cli.py --channel delivery --delivery-fee 9 --budget 45
    python cli.py --budget 30 --json               # 机器可读，给 Agent 用
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcd_saver import (  # noqa: E402
    Channel,
    Goal,
    Menu,
    Plan,
    SearchConfig,
    load_coupons,
    load_menu,
    money,
    optimize,
    recommend,
)
from mcd_saver.coupons import QuoteContext
from mcd_saver.loader import DataError, parse_coupons_arg
from mcd_saver.optimizer import Offer

ROOT = Path(__file__).resolve().parent
WIDTH = 62


def _parse_time_arg(raw: str | None):
    """把 "HH:MM" 解析成 time。时段券判断要用，所以格式错了宁可报错。"""
    if not raw:
        return None
    from datetime import datetime

    text = raw.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise SystemExit(f"时间格式应为 HH:MM，收到 {raw!r}")


def hr(char: str = "-") -> str:
    return char * WIDTH


def fmt_plan(offer: Offer, index: int | None = None) -> str:
    """把一条方案排成人能直接照着念的几行。"""
    plan = offer.plan
    prefix = f"[{offer.tag}] " if offer.tag else ""
    head = f"{index}. {prefix}" if index is not None else prefix
    lines = [f"{head}{offer.headline}"]

    detail = f"   小计 ¥{plan.subtotal:.2f}"
    if plan.coupon:
        detail += f" → 「{plan.coupon.name}」-¥{plan.discount:.2f}"
    else:
        detail += " → 无可用券"
    if plan.delivery_fee > 0:
        detail += f" + 配送 ¥{plan.delivery_fee:.2f}"
    detail += f"  = 实付 ¥{plan.pay:.2f}"
    lines.append(detail)

    saved = f"   省钱 ¥{plan.saved:.2f}（{plan.saving_ratio * 100:.1f}%）"
    if plan.calories > 0:
        saved += f"  |  {plan.calories:.0f} 大卡"
        saved += f"  |  每元 {plan.calories_per_yuan:.0f} 大卡"
    lines.append(saved)

    if offer.reason:
        lines.append(f"   >>> {offer.reason}")
    return "\n".join(lines)


def show_single(result, title: str) -> None:
    print(f"\n{title}")
    if not result.offers:
        print("   没有满足条件的方案。放松预算或热量约束再试。")
        return
    for i, offer in enumerate(result.offers[:3], 1):
        print(fmt_plan(offer, i))
        print()


def show_recommend(offers: list[Offer], title: str) -> None:
    print(f"\n{title}")
    if not offers:
        print("   没有满足条件的方案。")
        return
    for i, offer in enumerate(offers, 1):
        print(fmt_plan(offer, i))
        print()


def show_rejected(result) -> None:
    """券被排除的原因要摊开讲，不然用户会怀疑引擎漏了券。"""
    if not result.rejected:
        return
    print("   未采用的券：")
    seen: set[str] = set()
    count = 0
    for coupon, reason in result.rejected:
        key = f"{coupon.coupon_id}:{reason}"
        if key in seen:
            continue
        seen.add(key)
        print(f"     - {coupon.name}：{reason}")
        count += 1
        if count >= 6:
            break
    print()


def to_json(offers: list[Offer], result) -> str:
    payload = {
        "offers": [
            {
                "tag": o.tag,
                "items": [
                    {"sku": ln.sku, "name": ln.name, "qty": ln.qty, "unit_price": ln.unit_price}
                    for ln in o.plan.lines
                ],
                "coupon": o.plan.coupon.coupon_id if o.plan.coupon else None,
                "coupon_name": o.plan.coupon_name,
                "subtotal": o.plan.subtotal,
                "discount": o.plan.discount,
                "delivery_fee": o.plan.delivery_fee,
                "pay": o.plan.pay,
                "saved": o.plan.saved,
                "saving_ratio": round(o.plan.saving_ratio, 4),
                "calories": o.plan.calories,
                "calories_per_yuan": round(o.plan.calories_per_yuan, 1),
                "note": o.reason,
            }
            for o in offers
        ],
        "rejected_coupons": [
            {"coupon": c.name, "reason": r} for c, r in result.rejected[:12]
        ],
        "scanned": result.scanned,
        "elapsed_ms": round(result.elapsed_ms, 1),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def resolve_sku(menu: Menu, text: str) -> str:
    """把用户嘴里的「麦乐鸡」翻译成 sku。用户不会记 sku，所以按名字匹配。

    匹配优先级：sku 精确 > 商品名精确 > 商品名包含。前两级必须精确命中，
    否则「麦乐鸡10块」会被「麦乐鸡10块套餐」干扰。
    """
    exact = [m for m in menu if m.sku == text]
    if exact:
        return exact[0].sku
    same_name = [m for m in menu if m.name == text]
    if len(same_name) == 1:
        return same_name[0].sku
    hits = [m for m in menu if text in m.name]
    if len(hits) == 1:
        return hits[0].sku
    if not hits:
        raise SystemExit(f"菜单里没有包含「{text}」的商品")
    names = "、".join(f"{m.name}({m.sku})" for m in hits[:6])
    raise SystemExit(f"「{text}」匹配到多个：{names}\n请写得更具体一些")


def build_config(args, menu: Menu) -> SearchConfig:
    channel = Channel.parse(args.channel)
    today = date.today()
    now_time = _parse_time_arg(args.time)
    must = tuple(resolve_sku(menu, w) for w in args.want)
    return SearchConfig(
        budget=args.budget,
        max_calories=args.calories_max,
        min_calories=args.calories_min,
        goal=Goal.parse(args.goal),
        channel=channel,
        delivery_fee=args.delivery_fee if channel is Channel.DELIVERY else 0.0,
        now_time=now_time,
        today=today,
        max_addons=args.addons,
        top_k=args.top,
        store_id=args.store,
        must_include=must,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="麦麦省钱助手：算清每一单最划算的点法",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--budget", type=float, help="实付预算上限（元）")
    parser.add_argument("--calories-max", type=float, help="热量上限（大卡）")
    parser.add_argument("--calories-min", type=float, help="热量下限，用于「要吃饱」")
    parser.add_argument(
        "--goal",
        default="min_pay",
        help="min_pay(实付最低) / max_saving(省最多) / best_value(每元最扛饿) / max_ratio(省钱比例)",
    )
    parser.add_argument("--menu", default=str(ROOT / "data" / "sample_menu.json"))
    parser.add_argument("--coupons", default=str(ROOT / "data" / "sample_coupons.json"))
    parser.add_argument("--coupon", action="append", default=[], help='临时加券，如 "满30减5:30:5"')
    parser.add_argument("--want", action="append", default=[], help='必须包含的商品，如 "麦乐鸡"（可多次）')
    parser.add_argument("--channel", default="dine_in", help="dine_in / takeaway / delivery")
    parser.add_argument("--delivery-fee", type=float, default=9.0, help="配送费，仅麦乐送生效")
    parser.add_argument("--time", help="当前时间 HH:MM，用于判断时段券")
    parser.add_argument("--store", help="门店 ID，用于门店专属券")
    parser.add_argument("--addons", type=int, default=2, help="一个主体最多配几件补充品")
    parser.add_argument("--top", type=int, default=3, help="每类展示几条")
    parser.add_argument("--json", action="store_true", help="输出 JSON，供 Agent 直接消费")
    args = parser.parse_args(argv)

    try:
        menu_raw: Menu = load_menu(args.menu)
        coupons = load_coupons(args.coupons)
        if args.coupon:
            coupons.extend(parse_coupons_arg(args.coupon))
    except (DataError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"数据加载失败：{exc}", file=sys.stderr)
        return 2

    # 菜单要按当前时段切：过了 10:30 就不该还能点早餐品
    now_time = _parse_time_arg(args.time)
    menu = _menu_for_time(menu_raw, now_time)

    try:
        cfg = build_config(args, menu)
    except DataError as exc:
        print(f"参数有误：{exc}", file=sys.stderr)
        return 2

    if args.json:
        res = optimize(menu, coupons, cfg, cfg.now_time)
        picks = recommend(menu, coupons, cfg, cfg.now_time)
        print(to_json(picks or res.offers, res))
        return 0

    ctx = QuoteContext(
        channel=cfg.channel,
        store_id=cfg.store_id,
        now_time=cfg.now_time,
        today=cfg.today,
        delivery_fee=cfg.delivery_fee,
    )

    print(hr("="))
    bits = [f"菜单 {len(menu)} 项", f"券 {len(coupons)} 张", cfg.channel.label]
    if cfg.budget:
        bits.append(f"预算 ¥{cfg.budget:g}")
    if cfg.max_calories:
        bits.append(f"≤{cfg.max_calories:g} 大卡")
    if cfg.min_calories:
        bits.append(f"≥{cfg.min_calories:g} 大卡")
    if cfg.now_time:
        bits.append(cfg.now_time.strftime("%H:%M"))
    print("  ".join(bits))
    print(hr("="))

    if cfg.delivery_fee and cfg.channel is not Channel.DELIVERY:
        print("提示：非麦乐送渠道不收配送费，已忽略 --delivery-fee。")

    picks = recommend(menu, coupons, cfg, cfg.now_time)
    res = optimize(menu, coupons, cfg, cfg.now_time)

    title = "【多维推荐】同一笔钱，几种花法"
    if cfg.must_include:
        wanted = "、".join(menu[s].name for s in cfg.must_include)
        title = f"【围绕「{wanted}」的最优组合】"
    show_recommend(picks, title)

    if not picks:
        show_single(res, "【放宽条件后的备选】")
    print(hr())
    show_rejected(res)
    print(f"扫描 {res.scanned} 个候选组合，耗时 {res.elapsed_ms:.0f} ms")
    print("价格与券状态以麦当劳官方实时数据为准。下单前请再核对一次清单。")
    return 0


def _menu_for_time(menu: Menu, now_time) -> Menu:
    """模拟 query-meals 的行为：过了早餐时段，菜单里就不该有早餐品。

    真实接入时这一步由 MCP 完成（query-meals 返回的就是当前可售）。
    本地演示必须自己切，否则会算出「下午三点吃麦满分」这种下不了单的方案。
    """
    if now_time is None:
        return menu
    from datetime import time as _t

    if now_time < _t(10, 30):
        return menu
    return Menu([m for m in menu if "早餐" not in m.category])


def run_demo() -> None:
    """不传参数时的默认演示：四个真实会遇到的问题。"""
    menu_all = load_menu(ROOT / "data" / "sample_menu.json")
    coupons = load_coupons(ROOT / "data" / "sample_coupons.json")
    today = date.today()
    lunch = _parse_time_arg("12:30")
    morning = _parse_time_arg("09:15")

    scenarios = [
        (
            "场景 A：只有 30 块，想吃一顿正经的（12:30）",
            _menu_for_time(menu_all, lunch),
            SearchConfig(budget=30, today=today, now_time=lunch),
        ),
        (
            "场景 B：预算 30 且要吃饱（≥700 大卡）",
            _menu_for_time(menu_all, lunch),
            SearchConfig(budget=30, min_calories=700, today=today, now_time=lunch),
        ),
        (
            "场景 C：控热量 500 大卡以内，能省就省",
            _menu_for_time(menu_all, lunch),
            SearchConfig(max_calories=500, today=today, now_time=lunch),
        ),
        (
            "场景 D：早上 9:15，早餐券才生效",
            _menu_for_time(menu_all, morning),
            SearchConfig(budget=25, today=today, now_time=morning),
        ),
    ]

    print(hr("="))
    print("麦麦省钱助手  演示")
    print(hr("="))

    for title, menu, cfg in scenarios:
        picks = recommend(menu, coupons, cfg)
        res = optimize(menu, coupons, cfg)
        show_recommend(picks, title)
        show_rejected(res)
        print(hr())


if __name__ == "__main__":
    if len(sys.argv) == 1:
        run_demo()
    else:
        raise SystemExit(main())
