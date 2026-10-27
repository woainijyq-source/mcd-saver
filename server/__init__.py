"""AI 按量付费（A2M）支付服务端。

只负责三类动作：返回 402 账单、验付、履约确认。
业务方案由既有 `mcd_saver` 引擎生成，金额全部由引擎计算。
"""

from __future__ import annotations

__all__ = ["create_app"]


def create_app(*args, **kwargs):
    # 延迟导入，避免仅引用本包时拉起 Flask 依赖。
    from .app import create_app as _factory

    return _factory(*args, **kwargs)
