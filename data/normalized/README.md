# 标准化数据说明

本目录保存由原始响应确定性转换得到的结构化事实数据。标准化过程不得修改 `data/raw/`，也不得在字段含义不明确时猜测映射。

## 存储布局

一份或一组属于同一次查询的分页原始响应，对应一个不可覆盖的完整批次：

```text
data/normalized/runs/<source>/<YYYY>/<MM>/<DD>/<bundle_id>/
├── manifest.json
├── security_master.jsonl
├── market_bars_daily.jsonl
└── valuation_snapshots.jsonl
```

只有包含 `manifest.json` 的完整目录才是可用批次。批次清单记录全部原始分页、页码、抓取时间、原始记录 ID、记录数和内容哈希。再次处理同一组原始记录会得到相同 `bundle_id` 并被拒绝，避免静默覆盖已有结果。

财务响应允许把不同报告期的独立查询组合为一个批次：

```text
data/normalized/runs/<source>/<YYYY>/<MM>/<DD>/<bundle_id>/
├── manifest.json
├── financial_reports.jsonl
└── financial_facts.jsonl
```

财务批次 ID 由有序原始记录 ID、映射版本和标准化器版本确定。不同抓取时点的数据不会覆盖，能够保留后来修订或重新披露的版本。

## 当前三张核心表

### `security_master.jsonl`

每行表示一次观察到的证券身份信息。主键为：

```text
security_code + observed_date
```

当前包括证券代码、交易所后缀、证券简称、市场归属标签、上市日期和上市状态。`observed_date` 是抓取日期，不被误当作证券信息正式生效日期。

原始字段 `股票市场类型` 实际可能返回以分号分隔的多个市场归属标签，因此标准字段使用数组 `market_memberships`，而不是假设它始终是单一市场类型。

### `market_bars_daily.jsonl`

每行表示一个证券在一个交易日、一个复权口径下的日行情。主键为：

```text
security_code + trade_date + adjustment_type
```

当前包括不复权开盘价、最高价、最低价、收盘价、成交量和成交额。若来源响应中成交量或成交额缺失，保留为 `null`，不得补零。没有收盘价的未上市证券不生成日行情记录。

### `valuation_snapshots.jsonl`

每行表示一个证券在一个时点的估值快照。主键为：

```text
security_code + as_of_date
```

当前包括总市值和滚动市盈率 `pe_ttm`。负市盈率按来源事实保留，不在标准化层解释或过滤。来源没有估值数据的未上市证券不生成估值记录。

## 财务数据基础表

### `financial_reports.jsonl`

每行表示一个证券、一个报告期在一次原始抓取中观察到的报告版本。主键为：

```text
security_code + period_end + raw_record_id
```

记录包括报告类型、来源报告期标签、公告日期和 `available_from`。当前 `available_from` 等于来源返回的公告日期，且标准化器拒绝公告日在报告期结束日前或抓取时间后的记录。这是后续时点分析避免未来信息的基础。

### `financial_facts.jsonl`

每行只保存一个财务事实，采用长表结构。主键为：

```text
security_code + period_end + canonical_field_name + raw_record_id
```

`statement_type` 区分 `income_statement`、`balance_sheet` 和 `cash_flow_statement`。`value_nature` 区分期末时点值与年初至报告期末累计值。缺失项目仍生成事实记录，`value` 为 `null`、`value_status` 为 `missing_in_source`，不得补零。

长表结构使新增 iWencai 动态财务字段时无需修改宽表列结构，也能让同一报告期的多个抓取或修订版本并存。

## 数据血缘

每条记录保存：

- 原始快照路径和 `raw_record_id`；
- 来源、抓取时间、映射版本和标准化器版本；
- `field_lineage` 中每个标准字段对应的原始字段名；
- 原始日期、单位、复权口径、解析器版本和置信度；
- iWencai 返回的字段角色、类型、单位和时间戳。

未映射字段只记录在批次清单的 `unmapped_fields` 中，其原始值仍完整保存在 `data/raw/`，不会被猜测写入标准化表。

## 生成命令

```bash
python3 scripts/normalize_iwencai_market.py \
  <page-001-snapshot.json> \
  <page-002-snapshot.json> \
  ... \
  <page-056-snapshot.json>
```

多页批次必须具有相同来源、查询和字段映射，页码必须从 1 连续排列，证券主表总数必须等于来源报告的总记录数。转换器只读取已经保存的本地原始响应，不发起网络请求，也不生成衍生指标或投资判断。

财务批次生成命令：

```bash
python3 scripts/normalize_iwencai_financials.py \
  <annual-report-snapshot.json> \
  <quarterly-report-snapshot.json>
```

财务转换器同样只读取本地原始快照，不调用 iWencai，也不计算增长率、利润率、ROE、ROIC、自由现金流或投资评分。
