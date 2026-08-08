# 投资决策日志与复盘

决策日志把“当时知道什么、如何推理、为什么行动、以后用什么证据推翻 thesis”固定下来，防止事后重写记忆。系统只提供结构和一致性检查，最终决策权始终属于投资者本人。

## 工作流

```text
Investment Policy Statement
→ Market + Portfolio Screening
→ Research Queue
→ Deep Research
→ Bull / Base / Bear Valuation
→ Red Team
→ Portfolio Fit
→ Position Sizing
→ Investor Decision
→ Monitoring
→ Post-decision Review
```

研究档案必须把 `facts`、`inferences` 和 `judgments` 分开。每条 inference 要引用已记录的 fact ID；进入 `decision_ready` 前必须具备三种估值情景、红队挑战和 thesis breakers。

决策条目记录当时采用的事实、推断、判断、情景、组合适配、目标权重、执行计划、不确定性和监控计划。复盘条目记录哪些判断正确、哪些错误、过程错误以及运气与能力的区分。

## 隐私

未脱敏的决策、目标仓位、执行计划和复盘应保存在 `decision_journal/private/`。公开仓库只提交空模板或主动脱敏后的研究方法与复盘。

验证命令：

```bash
python3 scripts/validate_research_workflow.py research <research-case.json>
python3 scripts/validate_research_workflow.py decision <decision-entry.json>
python3 scripts/validate_research_workflow.py review <review-entry.json>
```

验证器只检查证据链和结构完整性，不评价证券，也不执行交易。
