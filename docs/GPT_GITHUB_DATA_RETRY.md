# 给 GPT 的 GitHub 大文件读取与重试说明

## 你为什么会读到空正文

本仓库中的完整筛选结果 `market_research_queue.jsonl` 大于 1 MiB。GitHub
Contents API 对 1–100 MB 文件需要使用 raw 下载方式；部分 GitHub 连接器能够返回
文件路径、blob SHA 和成功状态，但不会返回正文，此时 `content` 会是空字符串。
[GitHub 官方 API 说明](https://docs.github.com/en/rest/repos/contents?apiVersion=2022-11-28)

这只说明连接器没有取得大文件正文，不代表文件不存在、数据损坏或仓库没有生成结果。
不要把空正文当作业务事实，也不要据此推断某只证券没有 P0/P1 结果。

## 本仓库提供的可读副本

每个筛选运行目录都包含 `github_connector/`：

- `manifest.json`：小文件清单、来源文件 SHA、记录数和所有分片的 SHA-256；
- `market_research_queue_p0_p1.jsonl`：完整的 P0/P1 记录摘要；
- `market_research_queue.part-XXXX.jsonl`：完整队列的顺序分片；
- 每个文件最多 900 KiB，低于 GitHub 连接器常见的 1 MiB 正文边界。

截至 2026-08-10，当前适用的沪深主板筛选运行是：

```text
data/derived/runs/screening/2026/08/07/b8e8a88ce7d140110835/
```

## 重新读取步骤

1. 不要再次读取运行目录根部的大文件 `market_research_queue.jsonl`。
2. 先读取该运行的 `github_connector/manifest.json`。
3. 查询 P0/P1 证券时，读取清单指定的
   `github_connector/market_research_queue_p0_p1.jsonl`。
4. 查询 P2、Reject 或核对完整覆盖时，按照清单中的 `full_queue.partitions`
   顺序逐个读取分片；不要猜测分片数量或文件名。
5. 用 `security_code` 精确匹配证券，并同时报告：来源 bundle、筛选版本、
   `as_of_date`、`priority`、`rank` 和 `score`。
6. 如果某个小文件仍返回空正文，应明确报告具体路径和返回状态，不得把读取失败解释为
   “仓库无数据”。

## 用于验证读取是否恢复的样例

请从当前运行的 P0/P1 摘要中重新读取 `600886.SH`。正确读取时应能找到“国投电力”
这一行；必须以你实际读到的 JSON 字段为准进行回答，并说明读取的是连接器副本而非根部
大文件。

## 可直接复制给 GPT 的重试指令

```text
你上次读取 GitHub 中的 market_research_queue.jsonl 时正文为空。原因是该权威文件
超过 1 MiB，连接器虽然取到了文件路径和 blob SHA，却没有返回正文；这不代表数据缺失。

请现在重新尝试：
1. 先读取 docs/GPT_GITHUB_DATA_RETRY.md；
2. 再读取
   data/derived/runs/screening/2026/08/07/b8e8a88ce7d140110835/github_connector/manifest.json；
3. 查询 P0/P1 时读取该清单指定的 market_research_queue_p0_p1.jsonl；
4. 精确查找 security_code=600886.SH；
5. 返回 security_name、eligible、priority、rank、score、as_of_date、
   screening_version 和 source_bundle_id；
6. 引用你实际成功读取的小文件路径。如果正文仍为空，只报告读取失败路径，不得推断仓库无数据。
```

## 完整性边界

`github_connector/` 是权威队列的逐行无损分片和 P0/P1 子集，不替代根部权威文件。
清单中的 `source_table.sha256` 应与根部 `manifest.json` 记录的 SHA-256 一致；完整分片按顺序
拼接后应与权威 JSONL 逐字节相同。
