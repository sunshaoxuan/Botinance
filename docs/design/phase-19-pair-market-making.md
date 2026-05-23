# P19 设计文档：专业做市与库存偏移交易内核

## 目标
把当前以单边重建和单边减仓为主的执行结构，收敛为成对挂单、库存偏移、hanging order 保留、pair 净边际闸门和只读运维 API 的统一内核。

## 当前问题
- 低质量换手过多，扣除手续费后净边际为负。
- 日内换手预算过早触发，后续机会直接被冻结。
- 卖出后的回补逻辑与挂单生命周期脱节。
- 看板和远端排查缺少 pair 维度的结构化账本。

## P19 核心结构
1. `PolicyEngine`
   - 输出 `MARKET_MAKING / INVENTORY_REBALANCE / RISK_REDUCTION / RECOVERY_ENTRY / OBSERVE_ONLY`
2. `InventorySkewOrderProposalEngine`
   - 按目标仓位偏离和库存权重生成多档 pair 提案
3. `OrderProposalFilter`
   - 过滤低净边际、低金额、价格区间不满足、保护锁阻塞、重复挂单
4. `OrderExecutor`
   - 保持 GTC 限价单生命周期，支持 hanging order 保留和 spread 容差重定价
5. `PaperPortfolio`
   - 记录 `open_order_pairs / completed_order_pairs / pair_profitability_stats / pair_locks`

## 报价模型
- 默认每侧 5 档
- spread 档位
  - 0.35%
  - 0.55%
  - 0.80%
  - 1.10%
  - 1.50%
- 单档目标金额 `8000 JPY`
- 有效最小金额 `5000 JPY`

## pair 净边际
公式：

`gross_roundtrip_edge_pct - maker_fee_sell_pct - maker_fee_buy_pct - safety_buffer_pct`

默认：
- `MAKER_FEE_PCT=0.001`
- `PAIR_EDGE_SAFETY_BUFFER_PCT=0.0005`
- `MIN_PAIR_NET_EDGE_PCT=0.0045`

## 挂单保留
- pair 挂单进入 hanging order 语义
- 陈旧只记录，不直接撤单
- 允许重定价的条件
  - spread 偏离目标结构超容差
  - 保护锁生效
  - 资产不足

## 保护层
- `PAIR_LOCK_AFTER_STOP`
- `STOPLOSS_GUARD`
- `DRAWDOWN_GUARD`
- `LOW_PROFIT_PAIR_LOCK`

## 数据契约
### paper_state
- `open_order_pairs`
- `completed_order_pairs`
- `pair_locks`
- `pair_profitability_stats`

### order / fill
- `pair_id`
- `pair_role`
- `intended_counter_price`
- `expected_pair_net_edge_pct`
- `completed_pair_net_edge_pct`

### ops API
- `/api/ops/health`
- `/api/ops/summary`
- `/api/ops/pairs`
- `/api/ops/orders`

## 本阶段验收重点
- pair 提案只在净边际达标时进入挂单阶段
- 成交后对侧单默认保留
- 低利润 pair 能触发锁定
- 远端可通过只读接口直接观察成交、撤单、pair 统计和保护锁
