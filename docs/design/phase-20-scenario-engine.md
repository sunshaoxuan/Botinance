# P20 设计文档：多场景自适应交易策略内核

## 目标
P20 在 P19 的成对做市、库存偏移、保护锁和 GTC 限价单基础上增加场景层。目标是让 Boti 能区分震荡、上行扩散、上行回调、上行持有、上行衰竭、下行防守、急跌风险、下跌后恢复和低波动观望，并用不同订单模板进入同一套提案过滤和订单生命周期。

## 当前问题
- P19 的订单结构已经偏专业做市，但场景识别较粗。
- 上行行情容易只等待折价挂买，可能错过确认入场。
- 下行行情需要进一步区分普通弱势、防守试单和急跌风险。
- 旧的建仓、补仓、减仓入口需要继续被 policy 层约束，避免绕过 pair 净边际和保护锁。

## 新增结构
1. `ScenarioEngine`
   - 输入 1m、3m、5m、30m、1h K 线。
   - 计算 MA6、MA18、MA 扩散、斜率、ATR、成交量、当前价偏离公平价和库存状态。
   - 输出唯一 `ScenarioDecision`。
2. `InventorySkewOrderProposalEngine`
   - 读取 `ScenarioDecision`。
   - 按场景调整买卖许可、订单数量、买入折扣、触发源和提案原因。
3. `OrderProposalFilter`
   - 继续执行手续费、净边际、有效金额、库存、资产和保护锁过滤。
   - 对 `UPTREND_PROBE_ENTRY`、`UPTREND_PULLBACK_ENTRY`、`RECOVERY_AFTER_DROP` 做受控小仓位放行。
4. `TradingEngine`
   - 每轮获取 1m、3m、5m、30m、1h 多周期 K 线。
   - 将场景结果写入 `CycleReport`、`execution_result` 和 dashboard payload。
5. Dashboard
   - 实时页新增“场景判断”卡片。
   - 显示主场景、MA 扩散周期数、ATR、量能、允许动作、禁止动作和中文原因。

## 场景定义
### RANGE_MARKET_MAKING
震荡做市。继续使用 P19 的 5 档双边 pair 提案、库存偏移和 hanging order。

### UPTREND_PROBE_ENTRY
上行扩散确认。至少两个短周期中 MA6 高于 MA18 且扩散速度增加。允许小仓位确认买入，默认使用目标缺口的 25%。

### UPTREND_PULLBACK_ENTRY
上行回调。上行结构仍在，价格回到 MA18、VWAP 或公平价附近。买入锚点取 MA18、VWAP、公平价中的较低可成交区。

### UPTREND_HOLD_EXPANSION
上行持有。已有仓位时不急于止盈，卖单只在卖出区或库存明显超标时生成。

### UPTREND_EXHAUSTION_TAKE_PROFIT
上行衰竭。MA6 走平且 MA18 追近，暂停追买，允许分批减仓。

### DOWNTREND_DEFENSIVE
下行防守。降低买入积极度，只允许深折价小仓位试单，普通追买被禁止。

### PANIC_RISK_REDUCTION
急跌风险。只允许风险退出和保护锁，不生成普通买单。

### RECOVERY_AFTER_DROP
下跌后恢复。MA6 重新上穿 MA18 且成交量恢复，允许恢复建仓，首单默认不超过目标缺口 20%。

### LOW_VOL_OBSERVE
低波动观望。ATR 低于阈值时不新增订单，只维护已有 GTC 挂单。

## 订单模板规则
- 震荡场景继续使用 5 档双边 pair 做市。
- 上行确认和恢复建仓最多使用 2 档小仓位买入。
- 下行防守最多使用 1 档深折价买入。
- 上行持有可禁止普通买入，只保留超库存卖出。
- 低波动和急跌风险不新增普通订单。
- 所有非风险订单仍需满足 pair 净边际、有效金额和资产检查。

## 档位合并
P20 增加 `ORDER_TIER_MERGE_ENABLED`。当场景模板生成多档后，如果相邻档金额低于有效下单金额，则自动合并，避免出现无意义碎单。

默认：
- `ORDER_TIER_MERGE_ENABLED=true`
- `ORDER_TIER_MERGE_MIN_NOTIONAL=5000`

合并后的订单仍保留：
- `pair_id`
- `tier_index`
- `target_spread_pct`
- `expected_pair_net_edge_pct`
- 中文原因，包含被合并的档位序号

## 配置
```env
SCENARIO_ENGINE_ENABLED=true
TREND_PROBE_ENTRY_FRACTION=0.25
RECOVERY_ENTRY_FRACTION=0.20
UPTREND_EXPANSION_MIN_PERIODS=2
UPTREND_EXHAUSTION_GAP_PCT=0.0015
DOWNTREND_BUY_DISCOUNT_MULTIPLIER=1.8
LOW_VOL_ATR_PCT=0.0008
ORDER_TIER_MERGE_ENABLED=true
ORDER_TIER_MERGE_MIN_NOTIONAL=5000
```

## Runtime 和 API 字段
### CycleReport
- `scenario_decisions`

### PolicyDecision
- `scenario_decision`
- `merged_order_proposals`

### `/api/dashboard`
- `scenario_decision`
- `scenario_decisions`
- `policy_state_summary[].scenario_decision`
- `order_proposal_summary[].merged_proposals`

### `execution_result`
- `scenario_decision`
- `scenario_state`
- `scenario_reason_cn`
- `scenario_indicators`
- `scenario_order_templates`

## 旧入口约束
当 `POLICY_ENGINE_ENABLED=true` 且 `LEGACY_DIRECT_ORDER_FALLBACK=false` 时，旧的建仓、补仓、减仓入口只能生成诊断，不能绕过 policy 和 direction 层提交订单。

P20 的有效路径固定为：

`ScenarioEngine -> PolicyEngine -> InventorySkewOrderProposalEngine -> OrderProposalFilter -> OrderExecutor`

## 验收标准
- 震荡行情生成双边 pair 做市提案。
- 至少两个短周期 MA6 向上扩散时生成 `UPTREND_PROBE_ENTRY`。
- MA18 追近 MA6 且 MA6 走平时进入 `UPTREND_EXHAUSTION_TAKE_PROFIT`。
- 下行趋势不允许普通追买。
- 急跌风险不生成普通买单。
- 低波动只维护已有 GTC 挂单。
- 小档位订单会合并到有效金额以上。
- `python3 -m unittest discover -s tests` 全绿。

## 已验证结果
本阶段实现后，本地全量测试结果为：

```text
Ran 154 tests
OK
```
