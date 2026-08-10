# 投资组合管理

本目录直接保存 ChatGPT 和研究系统需要读取的脱敏组合记录，不再额外套一层 `public/` 目录。

## 当前有效数据

以下文件是当前持仓的正式记录，提交到 `main` 后可直接读取：

```text
portfolio/
├── holdings.csv          # 当前持仓与平均成本
├── transactions.csv      # 历史成交记录
└── execution_status.json # 最近一次执行状态与费用前现金
```

记录来自用户报告，不替代券商对账单。未知费用、税费和实时市值保持为空，不猜测补零。

验证命令：

```bash
python3 scripts/validate_portfolio.py \
  --holdings portfolio/holdings.csv \
  --transactions portfolio/transactions.csv
python3 scripts/validate_repository.py
```

## 模板与私密文件

- `holdings.template.csv`、`transactions.template.csv` 和其他 `*.template.*` 文件是空白格式模板，不是当前持仓，也不会填入真实数据。
- 券商账号、外部订单号、身份信息、未脱敏流水和对账单必须保存在 `portfolio/private/` 或 `*.private.*` 文件中；这些路径不会进入 Git。
- 公开的 `holdings.csv`、`transactions.csv` 和 `execution_status.json` 必须移除账户、订单和身份标识。

## 更新规则

用户报告持仓变化后，应更新上述三个正式文件，运行字段与仓库完整性验证，然后合并到 `main`，最后从 `main` 重新读取确认。

## 字段边界

- `quantity`、`average_cost` 和 `market_value` 必须为非负数；
- `target_weight` 是0至1的比例；
- 证券代码使用带交易所后缀的仓库标准格式；
- 持仓复核只生成研究候选，不构成自动交易指令。

`investment_policy.template.json` 用于在投资前明确目标、期限、风险承受能力、流动性需求、允许或禁止资产、集中度和再平衡规则。真实个人约束仍应保存在 `portfolio/private/`。
