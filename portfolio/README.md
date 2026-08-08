# 投资组合管理

本目录保存组合数据规范和可公开的脱敏研究卡片。真实持仓、交易流水、账户标签和未脱敏决策属于个人隐私，必须放在 `portfolio/private/` 或使用 `*.private.*` 文件名；这些路径已被 Git 忽略。

## 本地文件

```text
portfolio/private/
├── holdings.csv
├── transactions.csv
└── investment_cards/
    └── <security_code>.json
```

从 `holdings.template.csv`、`transactions.template.csv` 和 `investment_card.template.json` 复制空模板后填写。模板没有示例持仓或交易。

验证命令：

```bash
python3 scripts/validate_portfolio.py \
  --holdings portfolio/private/holdings.csv \
  --transactions portfolio/private/transactions.csv \
  --cards portfolio/private/investment_cards
```

验证器只输出文件、行号和字段错误，不回显持仓数量、成本或交易内容。它检查表头、必填字段、日期、数值范围、交易类型、投资卡字段和目标权重范围，不计算收益，也不作投资判断。

投资卡还要求投资者明确维护 `thesis_status`、`valuation_status` 和 `risk_status`。持仓复核工具只把这些人工状态与实际/目标权重组合成候选类别：

```bash
python3 scripts/classify_portfolio_review.py \
  --holdings portfolio/private/holdings.csv \
  --cards portfolio/private/investment_cards \
  --output portfolio/private/review.json
```

thesis 已破坏优先进入 `EXIT_candidate`，重大风险进入 `TRIM_candidate`；低于目标权重且 thesis 完整、估值有吸引力时才进入 `ADD_candidate`。信息不充分一律进入 `REVIEW`。这些仍是复核候选，不是自动交易指令。

## 字段边界

- `quantity`、`average_cost` 和 `market_value` 必须为非负数；
- `target_weight` 是 0—1 的比例；
- 证券代码使用仓库标准格式（例如带交易所后缀的代码），但验证器暂不猜测或补全后缀；
- 投资卡必须明确 thesis、why_now、关键假设、thesis breakers、催化剂、风险和估值框架；
- 任何公开输出必须先脱敏，且不得包含券商账号、外部订单号等账户标识。
