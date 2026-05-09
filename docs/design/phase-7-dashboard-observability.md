# Phase 7 设计文档：Botinance 界面收口

## 目标

在不引入前端构建链、不改交易逻辑、不改 `/api/dashboard` 既有字段语义的前提下，把原报告型 dashboard 收口为 Botinance 本地交易界面。页面默认进入 `实时交易`，用户不需要读取 JSON 即可理解当前行情、持仓、AI 裁决、风险线、回测表现和系统运行状态。

## 信息架构

页面采用单页五分页结构：

- `实时交易`：核心交易工作区，包含主周期 K 线、成交量、模拟成交点、AI 否决点、止损/止盈/跟踪止损线、当前持仓、未实现盈亏、AI 风险闸门、执行状态、证据来源、AI 决策时间线、最近模拟成交。
- `AI 决策`：展示 GPT-5.5 市场判断、行动偏向、风险提示、证据来源、规则信号与 AI 裁决之间的关系。AI 只负责否决或降风险，不创建新买点。
- `回测分析`：只消费 P6 标准文件，优先读取 `runtime_backtest_walk`，缺失时回退 `runtime_backtest_check`，展示总览指标、权益曲线、回撤曲线、segment 表和交易明细。
- `风险控制`：展示买入决策链路、最小成交额、预算、数量取整、AI 风险裁决、止损/止盈/跟踪止损、当前阻塞原因。
- `系统日志`：展示刷新轮/决策轮、新闻刷新状态、最近 runtime 周期、最近执行结果和数据源状态。

## 视觉系统

新版视觉采用明亮冷调金融风：

- 白色与浅蓝为主背景，去掉旧版暖色大 Hero 和松散卡片堆叠。
- 面板使用薄边框、轻玻璃质感和低强度阴影。
- 收益使用绿色，风险使用红色，提醒与否决使用少量珊瑚色。
- 圆角控制在 `8-10px`，保持工具型界面的清晰边界。
- 左侧 `icon rail`、顶部 `app bar`、顶部 `tab bar` 和主内容区组成固定应用壳。

## 数据契约

页面继续走 `/api/dashboard`，保持原字段兼容，并直接消费以下字段：

- `latest_report`
- `paper_state`
- `history`
- `recent_fills`
- `live_main_interval_bars`
- `live_trade_markers`
- `live_ai_veto_markers`
- `backtest_summary`
- `backtest_segments`
- `backtest_equity_curve`
- `backtest_trades`
- `backtest_manifest`

前端状态固定为：

- `activeTab`
- `lastPayloadSnapshot`
- `refreshMs = 5000`

## 图表规则

实时交易主图使用原生 canvas 绘制，不引入第三方图表库：

- 主图为主周期 K 线，不按刷新轮重复绘制伪 K 线。
- 底部叠加成交量柱，当前以 `live_main_interval_bars.sample_count` 或 `volume` 作为可视化高度来源。
- 仅对实际 `PAPER_FILLED` 事件绘制 `BUY / SELL` 标记。
- 有持仓时绘制 `stop_loss_price`、`take_profit_price`、`trailing_stop_price`。
- 当策略信号为 `BUY` 且 AI 风险闸门否决时绘制 AI 否决标记。

## 回测页约束

回测页不运行回测、不复算指标、不解释内部状态，只读取 P6 标准化结果文件：

- `summary.json`
- `segments.json`
- `equity_curve.csv`
- `trades.csv`
- `run_manifest.json`

如果文件不存在，页面展示空状态，API 不返回 500。

## 验收标准

- `python3 -m unittest discover -s tests` 全绿。
- 本地打开 dashboard 后默认进入 `实时交易`。
- 五个分页均可切换。
- 实时主图可显示 K 线、成交量、买卖点、退出线和 AI 否决点。
- 回测页可读取 `runtime_backtest_walk`，缺失时回退 `runtime_backtest_check`。
- 窄屏下主图、右侧面板、底部区块纵向排列，文字不溢出。
