# Phase 8 设计文档：卖出诊断、历史账本与仓位激活

## 目标

P8 将 Boti 从买入过滤器补全为完整持仓交易系统。现有规则退出、策略 `SELL`、纸面成交和盈亏计算继续保留；本阶段新增可解释卖出链路、历史决策账本和纸面仓位激活。

## 决策链路

每轮对每个交易对输出 `sell_diagnostics`：

- 无持仓：明确记录“不需要卖出”。
- 有持仓但无退出：记录止损、止盈、跟踪止损、持仓 K 线数和继续持有原因。
- 规则退出：优先触发 `stop_loss / take_profit / trailing_stop / max_hold_exit`。
- 策略卖出：其次触发 `strategy_sell`。
- 仓位激活：最后评估主动网格。

执行优先级固定为：规则退出、策略 `SELL`、仓位激活、普通买入、`HOLD`。

## 仓位激活

仓位激活只在 `DRY_RUN=true` 的纸面交易中启用。第一版采用主动网格，并限制为一个待回补 tranche，避免多层库存导致解释困难。

默认参数：

- `GRID_SELL_STEP_PCT=0.003`
- `GRID_BUYBACK_STEP_PCT=0.0025`
- `GRID_SELL_FRACTION=0.25`
- `GRID_MIN_CORE_POSITION_FRACTION=0.25`
- `GRID_MAX_DAILY_TRADES=8`
- `GRID_ALLOW_LOSS_RECOVERY_SELL=true`

触发语义：

- `grid_profit_sell`：浮盈达到阈值后卖出 25%。
- `grid_loss_recovery_sell`：浮亏时按标准比例卖出，等待更低价回补。
- `grid_buyback`：价格从最近网格卖出价回落到回补线后买回待回补数量。

所有网格成交仍走现有 `OrderExecutor` 和 `PaperPortfolio`，低于最小数量或最小成交额时直接阻塞并记录原因。

## 历史账本

每轮写入 `decision_ledger`，包括刷新轮和决策轮。账本记录时间、模式、交易对、价格、持仓、权益、买入判断、卖出判断、AI 风险、最终动作和执行结果。

Dashboard 的 `系统日志` 页直接展示账本，用于复盘任意历史时点 Boti 当时为什么买、为什么卖、为什么继续持有。
