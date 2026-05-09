# Phase 6 设计文档：回测与评估体系

## 目标

把策略调整从人工观察切换成可验证评估。

## 范围

- 回测框架
- walk-forward
- 交易指标统计

## 核心指标

- 总收益
- 最大回撤
- 胜率
- 盈亏比
- 持仓时长
- MFE / MAE

## 当前实现

- 独立 CLI：`binance_ai.backtest.main`
- 历史数据加载器：按 `symbol + interval + from + to` 做本地缓存
- 单交易对离线回测：复用现有策略、风控、退出规则
- walk-forward：固定 `90d train / 30d test / 30d step`
- 结果文件：
  - `summary.json`
  - `trades.csv`
  - `equity_curve.csv`
  - `segments.json`
  - `run_manifest.json`

## 固定边界

- 官方评估链路禁用新闻层和 LLM 风险闸门
- 成交价固定为主周期 K 线 `close`
- 不模拟手续费、滑点、部分成交
- 同一根主周期 K 线上禁止翻仓
- 训练窗只用于指标预热，不在训练窗内开仓

## Walk-forward 聚合

- `summary.json` 在 walk-forward 模式下输出测试段聚合结果
- `run_manifest.json` 保留 full-sample baseline
- `segments.json` 输出每个窗口的独立结果
- `beats_baseline` 当前按 `expectancy_per_trade >= baseline.expectancy_per_trade` 判断
