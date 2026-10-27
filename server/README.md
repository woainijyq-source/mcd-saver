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

密钥有两个来源，环境变量优先：

**方式一：`server/.env`（手动接入推荐）**

按支付宝文档的标准命名建 `server/.env`，进程启动时自动加载（零依赖解析，已存在的环境变量优先）：

```
AIPAY_APP_ID=
AIPAY_PUBLIC_KEY=          # 应用公钥
AIPAY_PRIVATE_KEY=         # 应用私钥 PKCS#8（Java 用）
AIPAY_PRIVATE_PKCS_KEY=    # 应用私钥 PKCS#1（Python 实际用这一把）
AIPAY_ALIPAY_PUBLIC_KEY=   # 支付宝公钥
AIPAY_SELLER_ID=           # 商户 ID，2088 开头
```

**方式二：`.alipay-sandbox.json`**

由 AI 付 Skill 的沙箱配置步骤写入，字段为 `appIds[0]` 下的 `appId` / `appPrivatePkcsKey` / `alipayPublicKey` / `pid`。

其他开关：

- `ALIPAY_GATEWAY`：默认沙箱网关，生产必须显式设为生产网关
- `ALIPAY_SERVICE_ID`：默认 `api_mock_service_id`，生产替换为服务市场真实 `serviceId`
- `ALIPAY_PRICE_AMOUNT`：单次调用价格，默认 `0.01`
- `MCD_SAVER_PORT`：监听端口，默认 `5000`

## 本机运行注意

**macOS 的 5000 端口默认被「隔空播放接收器」占用**（`ControlCenter` 进程），直接起服务会端口冲突或请求被系统接管（表现为莫名其妙地返回 403/404）。用别的端口：

```bash
MCD_SAVER_PORT=5001 .venv/bin/python -m server.app
```

**请求本地服务时注意代理**。如果 shell 里设了 `HTTP_PROXY`/`HTTPS_PROXY` 却没配 `no_proxy`，Python 的 `urllib` 会把 `127.0.0.1` 的请求也发给代理，得到 502：

```bash
no_proxy="127.0.0.1,localhost" curl http://127.0.0.1:5001/healthz
```
