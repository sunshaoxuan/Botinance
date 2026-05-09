# Phase 4 设计文档：AI 从解释层升级为约束层

## 目标

让 LLM 输出影响风险放行，而不是只做中文摘要。

## 范围

- `allow_entry`
- `risk_score`
- `position_multiplier`
- `veto_reason`

## 核心设计

- 规则策略先给基础信号
- AI 只允许做否决、缩仓、风险加注释
- AI 不允许绕过规则直接强制开仓

