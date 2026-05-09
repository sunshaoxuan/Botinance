# Phase 7 设计文档：看板与可观测性增强

## 目标

在不重做前端工程的前提下，把实时监控和 P6 回测结果收口到一个本地 dashboard，让用户不读 JSON 也能看懂系统状态。

## 范围

- 页头轻量切换条：`实时监控 / 回测分析`
- 实时主图升级为主周期 `K 线`
- 主图叠加：
  - `BUY / SELL` 成交标记
  - `止损 / 止盈 / 跟踪止损` 线
  - `AI 否决` 标记
- 实时页保留并强化：
  - 仓位卡片
  - 买入决策链路
  - AI 风险闸门
  - 刷新轮 / 决策轮状态
- 回测页直接消费 P6 标准文件：
  - `summary.json`
  - `segments.json`
  - `equity_curve.csv`
  - `trades.csv`
  - `run_manifest.json`

## 交付标准

- 不看 JSON 也能理解当前为什么持有、为什么没买、当前退出风险在哪里
- dashboard 默认优先读取 `runtime_backtest_walk`，缺失时回退到 `runtime_backtest_check`
- `/api/dashboard` 扩展输出：
  - `live_main_interval_bars`
  - `live_trade_markers`
  - `live_ai_veto_markers`
  - `backtest_summary`
  - `backtest_segments`
  - `backtest_equity_curve`
  - `backtest_trades`
  - `backtest_manifest`
