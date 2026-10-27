"""订单持久化端口（SQLite）。

履约的原子性靠三点保证：
1. `BEGIN IMMEDIATE` 事务，读校验与写入同一事务；
2. `trade_no` 唯一索引，同一笔支付不可能被两个订单重复消费；
3. 资源只有首次进入履约时生成一次，结果随订单落库，重试只复用已保存结果。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB_DIR = PROJECT_ROOT / "server" / "var"
DEFAULT_DB_PATH = DB_DIR / "orders.db"

ALLOWED_ORDER_STATUS = ("PENDING_PAYMENT", "PAID")
ALLOWED_FULFILL_STATUS = ("UNFULFILLED", "PENDING_CONFIRM", "FULFILLED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    out_trade_no    TEXT PRIMARY KEY,
    amount          TEXT NOT NULL,
    currency        TEXT NOT NULL,
    resource_id     TEXT NOT NULL,
    goods_name      TEXT NOT NULL,
    pay_before      TEXT NOT NULL,
    order_status    TEXT NOT NULL,
    fulfill_status  TEXT NOT NULL,
    trade_no        TEXT,
    service_result  TEXT,
    query_json      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_trade_no
    ON orders(trade_no) WHERE trade_no IS NOT NULL;
"""


class OrderError(RuntimeError):
    """订单状态或数据不允许继续。"""


class OrderNotFound(OrderError):
    pass


class TradeNoReused(OrderError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat()


class SqliteOrderRepository:
    """实现 create_pending / find_by_out_trade_no / prepare_fulfillment / mark_fulfilled。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def create_pending(self, order: Mapping[str, Any]) -> None:
        required = ("out_trade_no", "amount", "resource_id", "goods_name", "pay_before")
        missing = [key for key in required if not str(order.get(key) or "").strip()]
        if missing:
            raise OrderError(f"创建订单缺少字段: {', '.join(missing)}")
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT INTO orders (out_trade_no, amount, currency, resource_id, goods_name,
                                    pay_before, order_status, fulfill_status, query_json,
                                    created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order["out_trade_no"],
                    order["amount"],
                    order.get("currency", "CNY"),
                    order["resource_id"],
                    order["goods_name"],
                    order["pay_before"],
                    order.get("order_status", "PENDING_PAYMENT"),
                    order.get("fulfill_status", "UNFULFILLED"),
                    json.dumps(order.get("query") or {}, ensure_ascii=False),
                    _now(),
                    _now(),
                ),
            )

    def find_by_out_trade_no(self, out_trade_no: str) -> Optional[dict[str, Any]]:
        if not out_trade_no:
            return None
        with closing(self._connect()) as conn:
            row = conn.execute("SELECT * FROM orders WHERE out_trade_no = ?", (out_trade_no,)).fetchone()
        return dict(row) if row else None

    def prepare_fulfillment(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """原子校验订单并只生成一次资源。"""
        out_trade_no = str(payload.get("out_trade_no") or "")
        trade_no = str(payload.get("trade_no") or "")
        if not out_trade_no or not trade_no:
            raise OrderError("履约入参缺少订单号或交易号")
        create_resource: Callable[[], str] = payload["create_resource"]

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM orders WHERE out_trade_no = ?", (out_trade_no,)).fetchone()
                if row is None:
                    raise OrderNotFound("本地订单不存在")

                # 同一交易号已绑定其他订单：支付凭证被重复使用。
                other = conn.execute(
                    "SELECT out_trade_no FROM orders WHERE trade_no = ? AND out_trade_no <> ?",
                    (trade_no, out_trade_no),
                ).fetchone()
                if other is not None:
                    raise TradeNoReused("交易号已被其他订单使用")

                if row["trade_no"] and row["trade_no"] != trade_no:
                    raise OrderError("订单已绑定其他交易号")

                if row["fulfill_status"] in ("PENDING_CONFIRM", "FULFILLED"):
                    if not row["service_result"]:
                        raise OrderError("订单履约结果缺失")
                    return {"state": row["fulfill_status"], "service_result": row["service_result"]}

                if row["currency"] != "CNY":
                    raise OrderError("订单币种不是 CNY")
                if row["order_status"] not in ALLOWED_ORDER_STATUS:
                    raise OrderError("订单当前状态不允许履约")
                if row["fulfill_status"] not in ALLOWED_FULFILL_STATUS:
                    raise OrderError("订单履约状态不允许履约")

                expected_amount = payload.get("expected_amount")
                expected_resource = payload.get("expected_resource_id")
                if str(row["amount"]) != str(expected_amount):
                    raise OrderError("验付金额与本地订单不一致")
                if row["resource_id"] != expected_resource:
                    raise OrderError("验付资源与当前请求资源不一致")
                if not _is_future(row["pay_before"]):
                    raise OrderError("订单已超过支付截止时间")

                service_result = create_resource()
                if not service_result:
                    raise OrderError("资源生成结果为空")
                if not isinstance(service_result, str):
                    service_result = json.dumps(service_result, ensure_ascii=False)

                conn.execute(
                    """
                    UPDATE orders
                       SET order_status = 'PAID', trade_no = ?, fulfill_status = 'PENDING_CONFIRM',
                           service_result = ?, updated_at = ?
                     WHERE out_trade_no = ?
                    """,
                    (trade_no, service_result, _now(), out_trade_no),
                )
                conn.execute("COMMIT")
                return {"state": "PENDING_CONFIRM", "service_result": service_result}
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    def mark_fulfilled(self, out_trade_no: str, trade_no: str) -> None:
        with closing(self._connect()) as conn:
            cursor = conn.execute(
                """
                UPDATE orders
                   SET fulfill_status = 'FULFILLED', updated_at = ?
                 WHERE out_trade_no = ? AND trade_no = ? AND fulfill_status = 'PENDING_CONFIRM'
                """,
                (_now(), out_trade_no, trade_no),
            )
            if cursor.rowcount == 0:
                raise OrderError("订单未能标记为已履约")


def _is_future(value: str) -> bool:
    try:
        return datetime.fromisoformat(str(value)).timestamp() > datetime.now(timezone.utc).timestamp()
    except (TypeError, ValueError):
        return False
