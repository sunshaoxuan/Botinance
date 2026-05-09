# Phase 1 设计文档：交易正确性修复

## 目标

修正交易规则校验顺序，保证模拟成交与未来实盘在交易所约束上保持一致。

## 范围

- 风控层最终成交额校验
- 执行层二次校验
- 模拟撮合层二次校验
- 看板展示最终成交额

## 关键设计

### 风控层

先算原始数量，再按 `step_size` 向下量化，再用：

- `final_notional = adjusted_quantity * price`

重新判断是否满足：

- `min_qty`
- `min_notional`

### 执行层

执行前再次校验交易所规则，防止上游漏判。

### 模拟撮合层

`PaperPortfolio.apply_order()` 接受：

- `min_notional`
- `min_qty`

即使执行层漏判，也不允许非法 paper fill 落盘。

## 交付标准

- 不再出现低于最小成交额的成交
- `buy_diagnostics` 显示最终成交额与是否通过

