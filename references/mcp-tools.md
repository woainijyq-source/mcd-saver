# 麦当劳 MCP 工具清单与调用要点

数据来源：麦当劳中国官方 MCP Server（`https://mcp.mcd.cn`，Streamable HTTP）。
本技能只用到其中 11 个工具，其余（积分抽奖、商城、主题活动等）与本场景无关，不在此列出。

## 接入方式

```json
{
  "mcpServers": {
    "mcd-mcp": {
      "type": "streamablehttp",
      "url": "https://mcp.mcd.cn",
      "headers": { "Authorization": "Bearer YOUR_MCP_TOKEN" }
    }
  }
}
```

- Token 申请：<https://open.mcd.cn/mcp>（手机号登录 → 控制台 → 激活 → 复制）
- 限流：单 Token 600 次/分钟，超了返回 429
- 鉴权失败返回 401，检查 Authorization 头与 Token 是否过期

## 本技能用到的工具

### 时间与门店

| 工具 | 用途 | 什么时候必须调 |
|---|---|---|
| `now-time-info` | 返回当前完整时间 | 只要可能用到时段券（早餐卡、限时券）就必须先调 |
| `query-nearby-stores` | 查地址附近门店 | 到店 / 外带场景，确定去哪家 |
| `delivery-query-addresses` | 查可配送地址列表 | 麦乐送场景，没有地址就先建 |
| `delivery-query-stores` | 查收货地址附近可配送门店 | 麦乐送场景 |

### 券

| 工具 | 用途 | 注意 |
|---|---|---|
| `query-my-coupons` | 查账户下所有券 | 输出的券列表喂给引擎最优先 |
| `available-coupons` | 查麦麦省可领的券 | 可领 ≠ 已领，要区分 |
| `auto-bind-coupons` | 一键领取所有可领券 | **会真实改动账户，必须用户同意** |
| `query-store-coupons` | 查当前门店可用券 | 需要门店 ID，到店场景用 |

### 菜单与价格

| 工具 | 用途 | 注意 |
|---|---|---|
| `query-meals` | 查当前可售菜单 | 返回的就是当前时段可售，早餐品过了 10:30 自然消失 |
| `query-meal-detail` | 查餐品详情与套餐组成 | 需要看可替换项、加价规则时才调 |
| `calculate-price` | 算金额、优惠、配送费、应付总额 | 下单前用它和引擎结果交叉核对 |
| `list-nutrition-foods` | 查餐品营养成分 | 用户提热量、减脂时才调 |

### 账户与订单

| 工具 | 用途 | 注意 |
|---|---|---|
| `query-my-account` | 查积分余额 | 只查余额。真正积分兑换要 `mall-points-products` + `mall-create-order` |
| `create-order` | 创建订单 | **只在用户明确说下单后调** |
| `query-order` | 查订单状态 | 用户问「到哪了」时用 |
| `order-list` | 查历史订单 | 可做「你上次这么点贵了 X 元」的对比 |

## 常见坑

1. **别把可领的券当已领的券。** `available-coupons` 是待领列表，用户实际付款时用不上，必须先 `auto-bind-coupons`。
2. **`query-meals` 的返回结构会变。** 见过裸数组、`{"data": [...]}`、`{"categories":[{"items":[...]}]}` 三种包裹形态，
   `loader.menu_from_mcp` 都能吃，但字段名要靠宽容匹配，缺价格会直接抛 `DataError`。
3. **`calculate-price` 的入参是商品编码 + 券 ID**，不是商品名。用引擎算完再转成编码去核对。
4. **一单只吃一张券。** 官方 `calculate-price` 传多张券也只生效一张，不要设计叠加方案。
5. **积分余额不等于能省钱。** 只有 `mall-points-products` 里存在可兑换的券，积分才转化为优惠。
