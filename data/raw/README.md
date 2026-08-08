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

## 当前边界

- 已保存经公开查询或受控 OpenAPI 查询取得的 iWencai 原始响应；API 密钥、授权头和 Cookie 不进入快照或仓库。
- 保存工具本身仍只负责本地存档，不包含网络请求、账号登录或凭据管理逻辑。
- 字段审计同时支持网页表格响应和 OpenAPI 的 `datas` 响应，但不会修改原始 `payload`。
- 当前没有自动定时采集、投资筛选或投资判断逻辑。
