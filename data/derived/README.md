# 派生数据说明

本目录保存从标准化事实确定性计算得到的数据。派生数据不是原始事实，也不包含投资建议；每项结果必须能追溯到输入事实、公式版本和计算器版本。

## 财务指标批次

```text
data/derived/runs/iwencai/YYYY/MM/DD/<bundle_id>/
├── manifest.json
└── financial_metrics_<period_end>.jsonl
```

清单记录来源财务批次、来源时间范围、指标定义版本、覆盖数量、物理分区及 SHA-256。派生批次不可覆盖；来源财务批次或公式版本变化会生成新的批次 ID。

每条指标记录保留证券、报告期、公告可得日期、公式、是否年化、计算状态，以及分子和分母事实的原始记录 ID、快照路径和抓取时间。当前状态包括：

- `calculated`：输入有效，已计算；
- `missing_inputs`：至少一个输入事实缺失；
- `zero_denominator`：分母为零；
- `non_positive_denominator`：公式要求正分母但分母不是正数。

生成命令：

```bash
python3 scripts/derive_financial_metrics.py \
  data/normalized/runs/iwencai/YYYY/MM/DD/<bundle_id>/manifest.json
```

计算器会先验证来源财务事实分区的 SHA-256。当前五项指标只做同报告期比率，不年化、不跨期推断、不计算评分，也不作投资判断。

## 市场研究优先级

`data/derived/runs/screening/` 保存行情估值和财务指标连接后的全市场研究队列。每条记录保留原始输入值、各分项百分位、总分、资格原因、排名和 P0/P1/P2/Reject。筛选器先验证来源文件哈希，并拒绝使用 `available_from` 晚于筛选日的财务指标。

```bash
python3 scripts/screen_market_research_queue.py \
  <market-manifest.json> \
  <financial-metric-manifest.json>
```

`Reject` 仅表示不满足当前规则的输入资格，不是对证券的投资结论。当前版本尚未行业中性化，完整限制见批次验证报告和 `research_queue/README.md`。

## 高级财务指标

`derive_advanced_financial_metrics.py` 接收一个或多个完整财务批次，按证券和上年同一报告期计算收入增长、归母净利润增长、平均归母权益 ROE、平均投入资本 ROIC 和自由现金流。

```bash
python3 scripts/derive_advanced_financial_metrics.py \
  <prior-financial-manifest.json> \
  <current-financial-manifest.json>
```

缺少上年可比期、当前字段、历史字段或有效分母时，记录保留 `null` 和精确状态。当前仓库数据尚未覆盖全部高级输入，因此没有为全市场发布伪造的高级指标批次。
