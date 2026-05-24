# P21 多平台合约信号票选与本地策略融合

## 目标

P21 在 P20 场景引擎之后增加外部市场共识层。Boti 继续以现货 XRP/JPY 的本地行情、库存、手续费、保护锁和限价单生命周期为主，外部合约数据只作为方向和风险因子参与融合。

第一版接入公开 REST 数据，不需要 OKX 或 Bybit 账户密钥，不做合约交易。

## 数据源

默认映射：

| 本地交易对 | Binance Futures | OKX | Bybit |
| --- | --- | --- | --- |
| XRP/JPY | XRPUSDT | XRP-USDT-SWAP | XRPUSDT |

采集维度：

| 维度 | 用途 |
| --- | --- |
| 标记价和指数价 | 判断外部价格偏离 |
| 未平仓量 | 判断杠杆资金是否进入 |
| 资金费率 | 判断多空拥挤和过热风险 |
| 多空比 | 判断账户方向倾斜 |
| 主动买卖量 | 判断短期成交压力 |

任一来源失败会被标记为 `stale_or_unavailable`，可用来源会重新归一化权重。

## 投票规则

每个平台独立生成 `MarketSignalVote`：

| 输出 | 含义 |
| --- | --- |
| `BULLISH` | 价格偏强、OI 增加、多头或主动买入占优 |
| `BEARISH` | 价格偏弱、OI 增加、空头或主动卖出占优 |
| `RISK_OFF` | 资金费率极端、杠杆拥挤或价格反向 |
| `NEUTRAL` | 数据不足、分歧或低置信度 |

共识权重：

| 层级 | 权重 |
| --- | --- |
| 本地 P20 场景 | 60% |
| 外部共识 | 40% |

外部 40% 内部默认权重：

| 来源 | 权重 |
| --- | --- |
| Binance Futures | 40% |
| OKX | 30% |
| Bybit | 30% |

## 融合规则

融合位置固定为：

```text
ScenarioEngine -> ExternalMarketSignalEngine -> PolicyEngine -> OrderProposalFilter -> OrderLifecycle
```

行为边界：

| 外部共识 | 融合行为 |
| --- | --- |
| 与本地买入场景同向 | 提高买入模板尺寸上限 10% 到 20% |
| 与本地买入场景反向 | 降低买入模板尺寸 30% 到 50% |
| `RISK_OFF` | 暂停普通新买单，增强保护说明 |
| `NEUTRAL` | 沿用本地场景 |

外部共识不能绕过：

| 约束 |
| --- |
| `OrderProposalFilter` |
| `MIN_PAIR_NET_EDGE_PCT` |
| 保护锁 |
| GTC 限价单生命周期 |
| 实盘双保险 |

## Runtime 字段

新增字段：

| 字段 | 内容 |
| --- | --- |
| `external_signal_snapshots` | 三个平台的原始标准化快照 |
| `external_signal_votes` | 单平台投票结果 |
| `external_consensus` | 外部聚合共识 |
| `blended_scenario_decisions` | 本地场景和外部共识融合后的场景 |
| `external_signal_health` | 数据源可用性和延迟 |

看板实时页新增“外部共识”卡片，显示方向、置信度、可用来源、风险分和延迟。

## 配置

```env
EXTERNAL_SIGNAL_ENABLED=true
EXTERNAL_SIGNAL_REFRESH_SECONDS=60
EXTERNAL_SIGNAL_STALE_SECONDS=180
EXTERNAL_SIGNAL_LOCAL_WEIGHT=0.60
EXTERNAL_SIGNAL_EXTERNAL_WEIGHT=0.40
EXTERNAL_SIGNAL_SOURCES=binance_futures,okx,bybit
EXTERNAL_SYMBOL_BINANCE_FUTURES_XRPJPY=XRPUSDT
EXTERNAL_SYMBOL_OKX_XRPJPY=XRP-USDT-SWAP
EXTERNAL_SYMBOL_BYBIT_XRPJPY=XRPUSDT
EXTERNAL_SIGNAL_MIN_SOURCES=2
EXTERNAL_SIGNAL_CAN_CHANGE_DIRECTION=true
EXTERNAL_SIGNAL_CAN_TRIGGER_RISK_OFF=true
```

## 验收

1. 任一外部源失败时，本地 P20 策略继续运行。
2. 至少两个外部源可用时，生成外部共识。
3. 外部 `RISK_OFF` 时不生成普通新买单。
4. 外部强多不能绕过净边际和保护锁。
5. 看板和 `/api/dashboard` 可读出外部共识与融合后场景。
