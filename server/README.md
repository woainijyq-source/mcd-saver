# 按量付费服务端（A2M）

把「麦当劳省钱点单方案」作为按量付费资源出售：请求不带支付凭证时返回 402 账单，
带 `Payment-Proof` 时验付、履约确认并返回方案。金额与方案都由 `scripts/mcd_saver` 引擎计算。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r server/requirements.txt
.venv/bin/python -m server.app
```

- 资源入口：`GET/POST /api/plan`，可选参数 `budget`、`calories_max`、`channel`、`time`、`goal`、`must_include`
- 健康检查：`GET /healthz`
- 支付宝配置读取项目根目录 `.alipay-sandbox.json`（不读 cwd，不用环境变量传密钥）
- 订单与履约结果存 `server/var/orders.db`，`trade_no` 唯一约束保证同一笔支付只履约一次

## 协议要点

1. 402 响应头 `Payment-Needed` 为 Base64URL 账单，`protocol`/`method` 字段完整，商家私钥签名（RSA2）
2. 验付校验 `active=true`、金额、订单号、资源一致，且 `trade_no` 未被其他订单使用
3. 资源只在首次进入履约时生成一次，履约确认失败可用同一 `Payment-Proof` 重试，不会重复生成

## 环境变量

- `ALIPAY_GATEWAY`：默认沙箱网关，生产必须显式设为生产网关
- `ALIPAY_SERVICE_ID`：默认 `api_mock_service_id`，生产替换为服务市场真实 `serviceId`
- `ALIPAY_PRICE_AMOUNT`：单次调用价格，默认 `0.01`
- `MCD_SAVER_PORT`：监听端口，默认 `5000`
