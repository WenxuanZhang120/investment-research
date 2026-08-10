import snapshotJson from "@/generated/portfolio-snapshot.json";
import { auth } from "@/auth";
import { createDecision, loginWithGitHub, logout, updateDecision } from "@/app/actions";
import type { DecisionLog } from "@/db/schema";
import { getDecisionLogs } from "@/lib/decision-log";
import type { PortfolioSnapshot } from "@/lib/types";

export const dynamic = "force-dynamic";

const snapshot = snapshotJson as PortfolioSnapshot;

const decisionLabels: Record<string, string> = {
  BUY: "买入",
  WATCH: "观察",
  HOLD: "持有",
  ADD: "加仓",
  TRIM: "减仓",
  EXIT: "退出",
  REVIEW: "复盘",
};

const confidenceLabels: Record<string, string> = { LOW: "低", MEDIUM: "中", HIGH: "高" };
const statusLabels: Record<string, string> = {
  DRAFT: "草稿",
  ACTIVE: "跟踪中",
  REVIEWED: "已复盘",
  CLOSED: "已关闭",
};

const money = new Intl.NumberFormat("zh-CN", {
  style: "currency",
  currency: "CNY",
  minimumFractionDigits: 2,
});

function DecisionForm({
  action,
  entry,
  disabled,
}: {
  action: (formData: FormData) => Promise<void>;
  entry?: DecisionLog;
  disabled?: boolean;
}) {
  return (
    <form action={action} className="decision-form">
      {entry && <input type="hidden" name="id" value={entry.id} />}
      <div className="form-grid form-grid-four">
        <label>
          <span>决策日期</span>
          <input name="decisionDate" type="date" required defaultValue={entry?.decisionDate ?? snapshot.asOfDate} disabled={disabled} />
        </label>
        <label>
          <span>标的代码</span>
          <input name="securityCode" required list="held-codes" placeholder="例如 513500.SH" defaultValue={entry?.securityCode} disabled={disabled} />
        </label>
        <label>
          <span>标的名称</span>
          <input name="securityName" required list="held-names" placeholder="例如 博时标普500ETF" defaultValue={entry?.securityName} disabled={disabled} />
        </label>
        <label>
          <span>决策类型</span>
          <select name="decisionType" defaultValue={entry?.decisionType ?? "WATCH"} disabled={disabled}>
            {Object.entries(decisionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
      </div>
      <div className="form-grid form-grid-three narrative-grid">
        <label>
          <span>核心理由</span>
          <textarea name="reason" required rows={4} placeholder="这次判断基于什么？" defaultValue={entry?.reason} disabled={disabled} />
        </label>
        <label>
          <span>证据与触发条件</span>
          <textarea name="evidence" required rows={4} placeholder="支持判断的数据、事实或待验证信号" defaultValue={entry?.evidence} disabled={disabled} />
        </label>
        <label>
          <span>风险与反证</span>
          <textarea name="risks" required rows={4} placeholder="什么情况会证明这次判断是错的？" defaultValue={entry?.risks} disabled={disabled} />
        </label>
      </div>
      <div className="form-grid form-grid-four form-footer">
        <label>
          <span>信心程度</span>
          <select name="confidence" defaultValue={entry?.confidence ?? "MEDIUM"} disabled={disabled}>
            <option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option>
          </select>
        </label>
        <label>
          <span>计划复盘日</span>
          <input name="reviewDate" type="date" defaultValue={entry?.reviewDate ?? ""} disabled={disabled} />
        </label>
        <label>
          <span>状态</span>
          <select name="status" defaultValue={entry?.status ?? "ACTIVE"} disabled={disabled}>
            {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </label>
        <button className="primary-button submit-button" type="submit" disabled={disabled}>
          {entry ? "保存修改" : "记录决策"}
        </button>
      </div>
    </form>
  );
}

function LoginScreen() {
  return (
    <main className="login-shell">
      <div className="ambient ambient-one" /><div className="ambient ambient-two" />
      <section className="login-card">
        <div className="brand-mark">研</div>
        <p className="eyebrow">PRIVATE INVESTMENT WORKSPACE</p>
        <h1>让持仓和思考，<br />回到同一个地方。</h1>
        <p className="login-copy">从固定 GitHub 数据源读取持仓，在私有数据库中沉淀每一次投资决策。首版不计算收益曲线，也不补造缺失行情。</p>
        <div className="login-features">
          <span><i className="dot dot-green" />GitHub 持仓同步</span>
          <span><i className="dot dot-blue" />结构化决策日志</span>
          <span><i className="dot dot-amber" />数据状态可追溯</span>
        </div>
        <form action={loginWithGitHub}>
          <button className="github-button" type="submit"><span className="github-icon">◆</span>使用 GitHub 登录</button>
        </form>
        <p className="login-note">仅允许 GitHub 用户 <strong>WenxuanZhang120</strong> 访问</p>
      </section>
    </main>
  );
}

export default async function Home() {
  const session = await auth();
  const allowedLogin = process.env.ALLOWED_GITHUB_LOGIN ?? "WenxuanZhang120";
  if (session?.user?.githubLogin?.toLowerCase() !== allowedLogin.toLowerCase()) return <LoginScreen />;

  const logs = await getDecisionLogs();
  const coveredValue = snapshot.positions.reduce((sum, position) => sum + (position.marketValue ?? 0), 0);

  return (
    <main className="app-shell">
      <datalist id="held-codes">{snapshot.positions.map((position) => <option key={position.securityCode} value={position.securityCode} />)}</datalist>
      <datalist id="held-names">{snapshot.positions.map((position) => <option key={position.securityName} value={position.securityName} />)}</datalist>
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark small">研</span><div><strong>投资工作台</strong><span>个人研究档案</span></div></div>
        <nav>
          <a href="#holdings" className="active"><span>◫</span>持仓总览</a>
          <a href="#journal"><span>◇</span>决策日志</a>
          <a href="#data-status"><span>◎</span>数据状态</a>
        </nav>
        <div className="sidebar-foot">
          <div className="identity"><span className="avatar">W</span><div><strong>{session.user.name ?? "Wenxuan"}</strong><span>@{session.user.githubLogin}</span></div></div>
          <form action={logout}><button className="text-button" type="submit">退出登录</button></form>
        </div>
      </aside>

      <div className="content">
        <header className="topbar">
          <div><p className="eyebrow">PORTFOLIO / {snapshot.asOfDate}</p><h1>持仓总览</h1></div>
          <div className="source-pill"><i className="dot dot-green" />GitHub 数据已读取</div>
        </header>

        <section id="holdings" className="section-block">
          <div className="metric-grid">
            <article className="metric-card featured"><span>已执行本金（费用前）</span><strong>{money.format(snapshot.executedPrincipalBeforeFees)}</strong><small>来自 execution_status.json</small></article>
            <article className="metric-card"><span>预估剩余现金（费用前）</span><strong>{money.format(snapshot.estimatedRemainingCashBeforeFees)}</strong><small>手续费与税费尚未记录</small></article>
            <article className="metric-card"><span>持仓数量</span><strong>{snapshot.positionCount}<em> 个</em></strong><small>全部为用户报告成交</small></article>
            <article className="metric-card"><span>已覆盖持仓市值</span><strong>{money.format(coveredValue)}</strong><small>{snapshot.quoteCoverage.available}/{snapshot.quoteCoverage.total} 个标的有可用行情</small></article>
          </div>

          <div className="panel holdings-panel">
            <div className="panel-heading"><div><p className="eyebrow">POSITIONS</p><h2>当前持仓</h2></div><span className="as-of">持仓日期 {snapshot.asOfDate}</span></div>
            <div className="table-wrap">
              <table>
                <thead><tr><th>标的</th><th>数量</th><th>平均成本</th><th>成本金额</th><th>最新价</th><th>估算市值</th><th>行情状态</th></tr></thead>
                <tbody>
                  {snapshot.positions.map((position) => {
                    const change = position.latestPrice === null ? null : (position.latestPrice - position.averageCost) / position.averageCost;
                    return (
                      <tr key={position.securityCode}>
                        <td><div className="security"><span className="security-badge">{position.securityCode.slice(0, 2)}</span><div><strong>{position.securityName}</strong><small>{position.securityCode}</small></div></div></td>
                        <td>{position.quantity.toLocaleString("zh-CN")}</td>
                        <td>{money.format(position.averageCost)}</td>
                        <td>{money.format(position.costBasis)}</td>
                        <td>{position.latestPrice === null ? <span className="muted">—</span> : <div><strong>{money.format(position.latestPrice)}</strong><small className={change !== null && change >= 0 ? "positive" : "negative"}>{change === null ? "" : `${change >= 0 ? "+" : ""}${(change * 100).toFixed(2)}% vs. 成本`}</small></div>}</td>
                        <td>{position.marketValue === null ? <span className="muted">不可估算</span> : money.format(position.marketValue)}</td>
                        <td>{position.quoteStatus === "available" ? <span className="status success"><i className="dot dot-green" />{position.quoteDate}</span> : <span className="status warning"><i className="dot dot-amber" />行情待落库</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="panel-note">估算市值只使用仓库中已有且可追溯的最新行情；两个 ETF 暂无行情记录，因此不计入“已覆盖持仓市值”。</p>
          </div>
        </section>

        <section id="journal" className="section-block">
          <div className="section-heading"><div><p className="eyebrow">DECISION JOURNAL</p><h2>决策日志</h2><p>把结论、证据、风险和复盘时间放在同一条记录里。</p></div><span className={`status ${logs.available ? "success" : "warning"}`}><i className={`dot ${logs.available ? "dot-green" : "dot-amber"}`} />{logs.available ? "私有数据库已连接" : logs.message}</span></div>
          <div className="panel composer">
            <div className="panel-heading compact"><h3>记录新决策</h3><span>仅你本人可见</span></div>
            <DecisionForm action={createDecision} disabled={!logs.available} />
          </div>
          <div className="journal-list">
            {logs.entries.length === 0 ? (
              <div className="empty-state"><span>◇</span><h3>{logs.available ? "还没有决策日志" : "等待数据库连接"}</h3><p>{logs.available ? "第一条记录会成为未来复盘时最有价值的上下文。" : "连接 Neon 数据库后即可在这里新建和编辑私有日志。"}</p></div>
            ) : logs.entries.map((entry) => (
              <article className="journal-card" key={entry.id}>
                <div className="journal-top"><div><span className="decision-chip">{decisionLabels[entry.decisionType] ?? entry.decisionType}</span><span className="journal-date">{entry.decisionDate}</span><h3>{entry.securityName}<small>{entry.securityCode}</small></h3></div><span className="status neutral">{statusLabels[entry.status] ?? entry.status}</span></div>
                <div className="journal-content"><div><span>核心理由</span><p>{entry.reason}</p></div><div><span>证据</span><p>{entry.evidence}</p></div><div><span>风险与反证</span><p>{entry.risks}</p></div></div>
                <div className="journal-meta"><span>信心：{confidenceLabels[entry.confidence] ?? entry.confidence}</span><span>计划复盘：{entry.reviewDate ?? "未设置"}</span><span>更新：{entry.updatedAt.toLocaleDateString("zh-CN")}</span></div>
                <details className="edit-drawer"><summary>编辑这条记录</summary><DecisionForm action={updateDecision} entry={entry} /></details>
              </article>
            ))}
          </div>
        </section>

        <section id="data-status" className="section-block">
          <div className="section-heading"><div><p className="eyebrow">DATA LINEAGE</p><h2>数据状态</h2><p>每一个数字都说明它从哪里来，以及哪里仍然缺失。</p></div></div>
          <div className="status-grid">
            <article className="panel status-card"><span className="status-icon green">G</span><div><h3>持仓数据</h3><p>已从固定 GitHub 分支读取 {snapshot.positionCount} 条持仓。</p><dl><div><dt>仓库</dt><dd>{snapshot.fixedGitHubSource.repository}</dd></div><div><dt>分支</dt><dd>{snapshot.fixedGitHubSource.branch}</dd></div><div><dt>文件</dt><dd>{snapshot.fixedGitHubSource.holdingsFile}</dd></div><div><dt>截至</dt><dd>{snapshot.asOfDate}</dd></div></dl></div></article>
            <article className="panel status-card"><span className="status-icon amber">市</span><div><h3>行情覆盖</h3><p>{snapshot.quoteCoverage.available}/{snapshot.quoteCoverage.total} 个持仓有仓库内可追溯行情。</p><dl>{snapshot.positions.map((position) => <div key={position.securityCode}><dt>{position.securityCode}</dt><dd>{position.quoteDate ? `${position.quoteDate} · ${money.format(position.latestPrice ?? 0)}` : "待落库"}</dd></div>)}</dl></div></article>
            <article className="panel status-card"><span className={`status-icon ${logs.available ? "green" : "amber"}`}>私</span><div><h3>决策日志</h3><p>{logs.available ? `私有数据库已连接，当前共有 ${logs.entries.length} 条记录。` : logs.message}</p><dl><div><dt>访问控制</dt><dd>GitHub 单用户白名单</dd></div><div><dt>存储</dt><dd>Neon Postgres</dd></div><div><dt>读写验证</dt><dd>服务端逐次校验</dd></div></dl></div></article>
          </div>
          <div className="scope-note"><strong>首版范围说明</strong><span>收益曲线暂不展示；手续费与税费未知；缺失行情不会被推算。后续可在数据稳定后单独增加收益率与净值模块。</span></div>
        </section>
        <footer>投资工作台 · 数据用于个人研究与复盘，不构成投资建议。</footer>
      </div>
    </main>
  );
}
