# 标准化数据说明

本目录保存由原始响应确定性转换得到的结构化事实数据。标准化过程不得修改 `data/raw/`，也不得在字段含义不明确时猜测映射。

## 存储布局

每份原始响应对应一个不可覆盖的完整批次：

```text
data/normalized/runs/<source>/<YYYY>/<MM>/<DD>/<raw_record_id>/
├── manifest.json
├── security_master.jsonl
├── market_bars_daily.jsonl
└── valuation_snapshots.jsonl
```

只有包含 `manifest.json` 的完整目录才是可用批次。再次处理同一个 `raw_record_id` 会被拒绝，避免静默覆盖已有结果。

## 当前三张核心表

### `security_master.jsonl`

每行表示一次观察到的证券身份信息。主键为：

```text
security_code + observed_date
```

当前包括证券代码、证券简称和市场类型。`observed_date` 是抓取日期，不被误当作证券信息正式生效日期。

### `market_bars_daily.jsonl`

每行表示一个证券在一个交易日、一个复权口径下的日行情。主键为：

```text
security_code + trade_date + adjustment_type
```

当前真实响应只可靠提供了不复权收盘价，因此暂不填充开盘价、最高价、最低价、成交量和成交额。

### `valuation_snapshots.jsonl`

每行表示一个证券在一个时点的估值快照。主键为：

```text
security_code + as_of_date
```

当前包括总市值和滚动市盈率 `pe_ttm`。负市盈率按来源事实保留，不在标准化层解释或过滤。

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
  data/raw/iwencai/2026/08/08/20260808T171511408147+0800_004c42e90d64b30c62fc.json
```

转换器只读取已经保存的本地原始响应，不发起网络请求，也不生成衍生指标或投资判断。
