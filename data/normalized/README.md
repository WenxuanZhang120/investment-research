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
