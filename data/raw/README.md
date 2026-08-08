# 原始数据存储规范

本目录用于保存数据源的原始 JSON 响应。保存工具只负责存档和记录来源，不负责字段解析、指标计算或投资判断。

## 核心规则

1. 每次采集都创建一个新快照。
2. 已存在的原始快照不得覆盖、修改或复用文件名。
3. 原始响应保存在 `payload` 中，不进行业务字段转换。
4. 每个快照都必须记录数据源、原始查询、抓取时间、唯一标识和内容哈希。
5. 抓取时间必须包含时区，并统一转换为 `Asia/Shanghai`。
6. 查询日志只允许追加，用于定位每次查询对应的原始文件。

## 目录结构

```text
data/raw/
├── <source>/
│   └── YYYY/
│       └── MM/
│           └── DD/
│               └── <timestamp>_<record_id>.json
└── _query_log/
    └── YYYY/
        └── MM/
            └── DD.jsonl
```

每个原始快照采用以下结构：

```json
{
  "metadata": {
    "source": "iwencai",
    "query": "原始查询文本",
    "fetched_at": "2026-08-08T16:00:00.000000+08:00",
    "record_id": "唯一标识",
    "schema_version": 1,
    "payload_sha256": "内容哈希"
  },
  "payload": {}
}
```

`payload_sha256` 根据 JSON 数据值的规范化表示计算，用于检查内容是否发生变化。它不代表原始 HTTP 字节流的哈希。

## 保存本地响应

保存工具仅接收已经存在于本地的 JSON 文件，不会发起网络请求：

```bash
python3 scripts/save_raw_response.py \
  --source iwencai \
  --query "原始查询文本" \
  --input /path/to/response.json
```

成功后，命令会输出新快照的保存路径。重复使用完全相同的查询、内容和抓取时间时，工具会拒绝覆盖已有快照。

## 分页 OpenAPI 采集

财务采集器从环境变量读取凭据，将每一页在跨页校验前先保存为不可变快照。凭据和授权头不会写入原始数据：

```bash
IWENCAI_API_KEY=... python3 scripts/collect_iwencai_financials.py \
  --query "明确报告期和字段的问句"
```

如果数据源配额或网络在中途终止，已经保存的页不删除、不覆盖。恢复后可从下一页继续：

```bash
IWENCAI_API_KEY=... python3 scripts/collect_iwencai_financials.py \
  --query "与首次采集完全相同的问句" \
  --start-page 42
```

续采命令只校验本次页段；完整性由标准化器对同一问句的全部快照统一检查。只有页码从 1 连续到最后一页、总行数和证券代码唯一性均通过时，才会产生标准化批次。

网络错误、HTTP 429 和服务端错误最多重试两次；HTTP 401/403 等凭据、权限或配额错误不会重试，避免无意义消耗调用。错误恢复后应使用 `--start-page` 从缺失页继续。

高级财务数据使用版本化运行计划：

```bash
python3 scripts/run_financial_collection_plan.py status
python3 scripts/run_financial_collection_plan.py collect --job 2026q1_base_resume
python3 scripts/run_financial_collection_plan.py normalize --job 2026q1_base_resume
```

`status --require-complete` 可作为门禁。运行器拒绝存在重复页、不连续页、总数变化或页大小变化的任务；遇到额度中断时，下次 `collect` 自动从连续尾页之后恢复。

公告搜索同样从环境变量读取凭据，并把网关 JSON 响应完整保存在快照 `payload` 内：

```bash
IWENCAI_API_KEY=... python3 scripts/collect_iwencai_announcements.py \
  "A股 最近七日 股份回购公告" --size 10
```

公告采集器不筛选、不改写响应字段；成功状态和 `data` 结构的校验发生在快照保存之后。

财经新闻使用相同的 raw-first 边界，但网关频道固定为 `news`：

```bash
IWENCAI_API_KEY=... python3 scripts/collect_iwencai_news.py \
  "A股 最近七日 重要公司新闻" --size 10
```

新闻网关响应同样完整保存在 `payload`；正文、来源元数据和搜索相关性字段不会在 raw 层删除。

## 当前边界

- 已保存经公开查询或受控 OpenAPI 查询取得的 iWencai 原始响应；API 密钥、授权头和 Cookie 不进入快照或仓库。
- 本地保存工具仍只负责存档；独立财务采集器负责受控网络请求，两者职责分离。
- 字段审计同时支持网页表格响应和 OpenAPI 的 `datas` 响应，但不会修改原始 `payload`。
- raw 存储与采集工具不包含投资筛选或投资判断逻辑；仓库当前也未配置自动定时采集。

仓库级完整性检查可通过 `python3 scripts/validate_repository.py` 运行。它会验证所有 raw payload 哈希及对应查询日志，不修改任何数据。
