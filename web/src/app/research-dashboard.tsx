"use client";

import { useEffect, useMemo, useState } from "react";
import { createDecision, logout, updateDecision } from "@/app/actions";
import type { PortfolioPosition, PortfolioSnapshot } from "@/lib/types";
import type {
  FinancialPeriod,
  ResearchContentItem,
  ResearchContentType,
  ResearchSnapshot,
  ScreeningSecurity,
  SecurityFinancialResponse,
} from "@/lib/research-types";

type ViewKey = "overview" | "screening" | "portfolio" | "content" | "journal" | "data";

type DecisionEntry = {
  id: string;
  ownerLogin: string;
  decisionDate: string;
  securityCode: string;
  securityName: string;
  decisionType: string;
  reason: string;
  evidence: string;
  risks: string;
  confidence: string;
  reviewDate: string | null;
  status: string;
  createdAt: string;
  updatedAt: string;
};

type DashboardProps = {
  user: { name: string; login: string };
  portfolio: PortfolioSnapshot;
  research: ResearchSnapshot;
  logs: { available: boolean; message: string | null; entries: DecisionEntry[] };
};

const viewMeta: Record<ViewKey, { label: string; eyebrow: string; marker: string }> = {
  overview: { label: "研究总览", eyebrow: "RESEARCH DESK", marker: "01" },
  screening: { label: "全市场筛选", eyebrow: "MARKET SCREENER", marker: "02" },
  portfolio: { label: "我的持仓", eyebrow: "PORTFOLIO", marker: "03" },
  content: { label: "内容中心", eyebrow: "INTELLIGENCE", marker: "04" },
  journal: { label: "决策日志", eyebrow: "DECISION JOURNAL", marker: "05" },
  data: { label: "数据状态", eyebrow: "DATA LINEAGE", marker: "06" },
};

const decisionLabels: Record<string, string> = {
  BUY: "买入", WATCH: "观察", HOLD: "持有", ADD: "加仓", TRIM: "减仓", EXIT: "退出", REVIEW: "复盘",
};
const confidenceLabels: Record<string, string> = { LOW: "低", MEDIUM: "中", HIGH: "高" };
const statusLabels: Record<string, string> = { DRAFT: "草稿", ACTIVE: "跟踪中", REVIEWED: "已复盘", CLOSED: "已关闭" };
const contentLabels: Record<ResearchContentType | "all", string> = {
  all: "全部", daily: "日报", news: "新闻", announcement: "公告", report: "研究报告",
};

const money = new Intl.NumberFormat("zh-CN", { style: "currency", currency: "CNY", minimumFractionDigits: 2 });
const compactNumber = new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 1 });

function formatMarketCap(value: number | null) {
  if (value === null) return "—";
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(value >= 10_000_000_000 ? 0 : 1)} 亿`;
  return compactNumber.format(value);
}

function formatPercent(value: number | null, digits = 1) {
  if (value === null || !Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

function formatMetric(value: number | null | undefined, kind: "money" | "ratio" = "money") {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return kind === "ratio" ? formatPercent(value) : formatMarketCap(value);
}

function priorityClass(priority: string) {
  return priority === "P0" ? "priority-p0" : priority === "P1" ? "priority-p1" : priority === "P2" ? "priority-p2" : "priority-reject";
}

function DecisionForm({ entry, disabled, asOfDate }: { entry?: DecisionEntry; disabled?: boolean; asOfDate: string }) {
  return (
    <form action={entry ? updateDecision : createDecision} className="decision-form light-form">
      {entry && <input type="hidden" name="id" value={entry.id} />}
      <div className="form-grid form-grid-four">
        <label><span>决策日期</span><input name="decisionDate" type="date" required defaultValue={entry?.decisionDate ?? asOfDate} disabled={disabled} /></label>
        <label><span>标的代码</span><input name="securityCode" required list="all-security-codes" placeholder="例如 600919.SH" defaultValue={entry?.securityCode} disabled={disabled} /></label>
        <label><span>标的名称</span><input name="securityName" required list="all-security-names" placeholder="例如 江苏银行" defaultValue={entry?.securityName} disabled={disabled} /></label>
        <label><span>决策类型</span><select name="decisionType" defaultValue={entry?.decisionType ?? "WATCH"} disabled={disabled}>{Object.entries(decisionLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <div className="form-grid form-grid-three narrative-grid">
        <label><span>核心理由</span><textarea name="reason" required rows={4} placeholder="这次判断基于什么？" defaultValue={entry?.reason} disabled={disabled} /></label>
        <label><span>证据与触发条件</span><textarea name="evidence" required rows={4} placeholder="支持判断的数据、事实或待验证信号" defaultValue={entry?.evidence} disabled={disabled} /></label>
        <label><span>风险与反证</span><textarea name="risks" required rows={4} placeholder="什么情况会证明判断是错的？" defaultValue={entry?.risks} disabled={disabled} /></label>
      </div>
      <div className="form-grid form-grid-four form-footer">
        <label><span>信心程度</span><select name="confidence" defaultValue={entry?.confidence ?? "MEDIUM"} disabled={disabled}><option value="LOW">低</option><option value="MEDIUM">中</option><option value="HIGH">高</option></select></label>
        <label><span>计划复盘日</span><input name="reviewDate" type="date" defaultValue={entry?.reviewDate ?? ""} disabled={disabled} /></label>
        <label><span>状态</span><select name="status" defaultValue={entry?.status ?? "ACTIVE"} disabled={disabled}>{Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
        <button className="primary-action submit-button" type="submit" disabled={disabled}>{entry ? "保存修改" : "记录决策"}</button>
      </div>
    </form>
  );
}

function EmptyState({ title, copy }: { title: string; copy: string }) {
  return <div className="empty-state"><span className="empty-mark">—</span><h3>{title}</h3><p>{copy}</p></div>;
}

function SecurityDrawer({ security, research, onClose, onOpenJournal }: { security: ScreeningSecurity; research: ResearchSnapshot; onClose: () => void; onOpenJournal: () => void }) {
  const [tab, setTab] = useState<"market" | "financial" | "score" | "related">("market");
  const [periods, setPeriods] = useState<FinancialPeriod[] | null>(null);
  const [loading, setLoading] = useState(true);
  const quote = research.quotes[security.securityCode];
  const related = research.content.filter((item) => item.securityCode === security.securityCode || (item.securityName && item.securityName === security.securityName));

  useEffect(() => {
    let active = true;
    fetch(`/api/security/${encodeURIComponent(security.securityCode)}`)
      .then((response) => response.ok ? response.json() as Promise<SecurityFinancialResponse> : Promise.reject(new Error("request failed")))
      .then((data) => { if (active) setPeriods(data.periods); })
      .catch(() => { if (active) setPeriods([]); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [security.securityCode]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const components = [
    ["市值", security.scoreComponents.marketCap], ["估值", security.scoreComponents.peTtm],
    ["净利率", security.scoreComponents.netProfitMargin], ["现金流", security.scoreComponents.operatingCashFlowMargin],
  ] as const;

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
      <aside className="detail-drawer" role="dialog" aria-modal="true" aria-label={`${security.securityName}证券详情`}>
        <div className="drawer-head">
          <div><div className="drawer-kicker"><span className={`priority-chip ${priorityClass(security.priority)}`}>{security.rank ? security.priority : "持仓"}</span><span>{security.securityCode}</span></div><h2>{security.securityName}</h2><p>{security.rank ? `全市场第 ${security.rank} 名 · 综合评分 ${(security.score * 100).toFixed(1)}` : "该标的不在当前 A 股筛选结果内"}</p></div>
          <button className="icon-button" onClick={onClose} aria-label="关闭证券详情">×</button>
        </div>
        <div className="drawer-tabs" role="tablist">
          {(["market", "financial", "score", "related"] as const).map((key) => <button key={key} className={tab === key ? "active" : ""} onClick={() => setTab(key)}>{key === "market" ? "行情" : key === "financial" ? "财务" : key === "score" ? "评分" : "关联内容"}</button>)}
        </div>
        <div className="drawer-body">
          {tab === "market" && <>
            <div className="detail-metrics">
              <article><span>最新收盘</span><strong>{quote ? money.format(quote.close) : "—"}</strong><small>{quote?.tradeDate ?? "行情未入库"}</small></article>
              <article><span>市盈率 TTM</span><strong>{security.peTtm === null ? "—" : security.peTtm.toFixed(2)}</strong><small>截至 {research.asOfDate}</small></article>
              <article><span>总市值</span><strong>{formatMarketCap(security.marketCap)}</strong><small>人民币</small></article>
              <article><span>研究优先级</span><strong>{security.rank ? security.priority : "—"}</strong><small>{security.eligible ? "通过初筛" : "未通过 / 不适用"}</small></article>
            </div>
            <div className="coverage-note"><strong>数据说明</strong><p>行情与估值均来自仓库内已落库快照，不使用浏览器实时行情，也不会为缺失字段补造数值。</p></div>
          </>}
          {tab === "financial" && <>
            {loading ? <div className="loading-block">正在读取财务数据…</div> : !periods?.length ? <EmptyState title="暂无财务数据" copy="ETF 或当前数据范围外证券不会显示上市公司财务指标。" /> : <div className="financial-periods">
              {periods.slice().reverse().map((period) => <article className="financial-card" key={period.periodEnd}>
                <div className="financial-card-head"><div><strong>{period.reportLabel}</strong><span>{period.periodEnd}</span></div><small>{period.filingDate ? `披露 ${period.filingDate}` : "披露日缺失"}</small></div>
                <dl>
                  <div><dt>营业收入</dt><dd>{formatMetric(period.facts.revenue)}</dd></div>
                  <div><dt>归母净利润</dt><dd>{formatMetric(period.facts.net_income_parent)}</dd></div>
                  <div><dt>经营现金流</dt><dd>{formatMetric(period.facts.net_cash_flow_operating)}</dd></div>
                  <div><dt>货币资金</dt><dd>{formatMetric(period.facts.monetary_funds)}</dd></div>
                  <div><dt>净利率</dt><dd>{formatMetric(period.metrics.net_profit_margin, "ratio")}</dd></div>
                  <div><dt>资产负债率</dt><dd>{formatMetric(period.metrics.liability_to_assets, "ratio")}</dd></div>
                </dl>
              </article>)}
            </div>}
          </>}
          {tab === "score" && <>
            <div className="score-hero"><span>综合评分</span><strong>{security.rank ? (security.score * 100).toFixed(1) : "—"}</strong><small>{security.rank ? `排名 ${security.rank} / ${research.coverage.screeningTotal}` : "未参与评分"}</small></div>
            <div className="score-bars">{components.map(([label, value]) => <div key={label}><div><span>{label}</span><strong>{value === null ? "—" : (value * 100).toFixed(1)}</strong></div><span className="score-track"><i style={{ width: `${Math.max(0, Math.min(100, (value ?? 0) * 100))}%` }} /></span></div>)}</div>
            {security.eligibilityReasons.length > 0 && <div className="coverage-note warning-note"><strong>未通过原因</strong><p>{security.eligibilityReasons.join("；")}</p></div>}
          </>}
          {tab === "related" && (related.length ? <div className="related-list">{related.map((item) => <article key={item.id}><span>{contentLabels[item.type]}</span><div><strong>{item.title}</strong><p>{item.summary}</p><small>{item.date.slice(0, 10)} · {item.source}</small></div></article>)}</div> : <EmptyState title="暂无关联内容" copy="当前新闻、公告和内部报告中没有与该证券直接关联的记录。" />)}
        </div>
        <div className="drawer-foot"><button className="secondary-action" onClick={onOpenJournal}>为这只证券记录决策</button><button className="primary-action" onClick={onClose}>完成查看</button></div>
      </aside>
    </div>
  );
}

function ContentReader({ item, onClose }: { item: ResearchContentItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return <div className="drawer-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><article className="reader-drawer" role="dialog" aria-modal="true" aria-label={item.title}>
    <header><div><span className={`content-type type-${item.type}`}>{contentLabels[item.type]}</span><h2>{item.title}</h2><p>{item.date.slice(0, 10)} · {item.source}</p></div><button className="icon-button" onClick={onClose} aria-label="关闭阅读器">×</button></header>
    <div className="reader-content">{item.content ? <pre>{item.content}</pre> : <><p className="reader-summary">{item.summary}</p>{item.securityName && <div className="reader-meta"><span>关联证券</span><strong>{item.securityName} {item.securityCode}</strong></div>}</>}</div>
    <footer>{item.url ? <a className="primary-action link-action" href={item.url} target="_blank" rel="noreferrer">打开原始来源</a> : <span className="source-note">内容来自仓库内已归档文档</span>}<button className="secondary-action" onClick={onClose}>关闭</button></footer>
  </article></div>;
}

function securityFromHolding(position: PortfolioPosition): ScreeningSecurity {
  return {
    securityCode: position.securityCode, securityName: position.securityName, rank: 0, priority: "Reject", score: 0, eligible: false,
    eligibilityReasons: ["该标的不在当前 A 股全市场筛选范围内"], marketCap: null, peTtm: null, netProfitMargin: null,
    operatingCashFlowMargin: null, financialPeriodEnd: null,
    scoreComponents: { marketCap: null, peTtm: null, netProfitMargin: null, operatingCashFlowMargin: null },
  };
}

export default function ResearchDashboard({ user, portfolio, research, logs }: DashboardProps) {
  const [view, setView] = useState<ViewKey>("overview");
  const [globalSearch, setGlobalSearch] = useState("");
  const [selectedSecurity, setSelectedSecurity] = useState<ScreeningSecurity | null>(null);
  const [selectedContent, setSelectedContent] = useState<ResearchContentItem | null>(null);
  const [screenQuery, setScreenQuery] = useState("");
  const [priority, setPriority] = useState("all");
  const [eligibility, setEligibility] = useState("all");
  const [minScore, setMinScore] = useState(0);
  const [capFilter, setCapFilter] = useState("all");
  const [sortKey, setSortKey] = useState("rank");
  const [page, setPage] = useState(1);
  const [contentType, setContentType] = useState<ResearchContentType | "all">("all");
  const [contentQuery, setContentQuery] = useState("");
  const pageSize = 50;

  const securityByCode = useMemo(() => new Map(research.securities.map((security) => [security.securityCode, security])), [research.securities]);
  const globalMatches = useMemo(() => {
    const query = globalSearch.trim().toLowerCase();
    if (!query) return [];
    return research.securities.filter((item) => item.securityCode.toLowerCase().includes(query) || item.securityName.toLowerCase().includes(query)).slice(0, 8);
  }, [globalSearch, research.securities]);

  const filteredSecurities = useMemo(() => {
    const query = screenQuery.trim().toLowerCase();
    const result = research.securities.filter((item) => {
      if (query && !item.securityCode.toLowerCase().includes(query) && !item.securityName.toLowerCase().includes(query)) return false;
      if (priority !== "all" && item.priority !== priority) return false;
      if (eligibility === "eligible" && !item.eligible) return false;
      if (eligibility === "ineligible" && item.eligible) return false;
      if (item.score * 100 < minScore) return false;
      if (capFilter === "mega" && (item.marketCap ?? 0) < 100_000_000_000) return false;
      if (capFilter === "large" && ((item.marketCap ?? 0) < 30_000_000_000 || (item.marketCap ?? 0) >= 100_000_000_000)) return false;
      if (capFilter === "small" && (item.marketCap ?? 0) >= 30_000_000_000) return false;
      return true;
    });
    return result.sort((a, b) => {
      if (sortKey === "score") return b.score - a.score;
      if (sortKey === "pe") return (a.peTtm ?? Number.MAX_SAFE_INTEGER) - (b.peTtm ?? Number.MAX_SAFE_INTEGER);
      if (sortKey === "marketCap") return (b.marketCap ?? -1) - (a.marketCap ?? -1);
      if (sortKey === "margin") return (b.netProfitMargin ?? -Infinity) - (a.netProfitMargin ?? -Infinity);
      return a.rank - b.rank;
    });
  }, [research.securities, screenQuery, priority, eligibility, minScore, capFilter, sortKey]);

  const totalPages = Math.max(1, Math.ceil(filteredSecurities.length / pageSize));
  const visibleSecurities = filteredSecurities.slice((page - 1) * pageSize, page * pageSize);
  const filteredContent = useMemo(() => {
    const query = contentQuery.trim().toLowerCase();
    return research.content.filter((item) => (contentType === "all" || item.type === contentType) && (!query || `${item.title} ${item.summary} ${item.securityName ?? ""}`.toLowerCase().includes(query)));
  }, [research.content, contentType, contentQuery]);
  const topCandidates = research.securities.filter((item) => item.priority === "P0").slice(0, 8);
  const coveredValue = portfolio.positions.reduce((sum, position) => sum + (position.marketValue ?? 0), 0);

  const openSecurity = (security: ScreeningSecurity) => { setSelectedSecurity(security); setGlobalSearch(""); };
  const openHolding = (position: PortfolioPosition) => openSecurity(securityByCode.get(position.securityCode) ?? securityFromHolding(position));
  const navigate = (nextView: ViewKey) => { setView(nextView); window.scrollTo({ top: 0, behavior: "smooth" }); };

  return <main className="workspace-shell">
    <datalist id="all-security-codes">{research.securities.slice(0, 600).map((item) => <option key={item.securityCode} value={item.securityCode} />)}</datalist>
    <datalist id="all-security-names">{research.securities.slice(0, 600).map((item) => <option key={item.securityName} value={item.securityName} />)}</datalist>
    <aside className="workspace-sidebar">
      <div className="workspace-brand"><span>IR</span><div><strong>投资研究台</strong><small>PRIVATE RESEARCH OS</small></div></div>
      <nav>{(Object.keys(viewMeta) as ViewKey[]).map((key) => <button key={key} className={view === key ? "active" : ""} onClick={() => navigate(key)}><span>{viewMeta[key].marker}</span>{viewMeta[key].label}{key === "screening" && <em>{research.coverage.screeningTotal.toLocaleString("zh-CN")}</em>}</button>)}</nav>
      <div className="sidebar-coverage"><span>数据更新</span><strong>{research.asOfDate}</strong><small>新闻与日报更新至 2026-08-09</small></div>
      <div className="sidebar-user"><span className="user-avatar">{user.name.slice(0, 1).toUpperCase()}</span><div><strong>{user.name}</strong><small>@{user.login}</small></div><form action={logout}><button type="submit" aria-label="退出登录">退出</button></form></div>
    </aside>

    <section className="workspace-main">
      <header className="workspace-topbar">
        <div><span>{viewMeta[view].eyebrow}</span><strong>{viewMeta[view].label}</strong></div>
        <div className="global-search-wrap"><label className="global-search"><span>⌕</span><input value={globalSearch} onChange={(event) => setGlobalSearch(event.target.value)} placeholder="搜索证券代码或名称" aria-label="全局证券搜索" /><kbd>⌘ K</kbd></label>{globalMatches.length > 0 && <div className="search-popover">{globalMatches.map((item) => <button key={item.securityCode} onClick={() => openSecurity(item)}><span><strong>{item.securityName}</strong><small>{item.securityCode}</small></span><em>{item.priority} · {(item.score * 100).toFixed(1)}</em></button>)}</div>}</div>
        <div className="topbar-status"><i />数据已就绪</div>
      </header>

      <div className="view-container">
        {view === "overview" && <section className="dashboard-view">
          <div className="hero-row"><div><p className="section-eyebrow">GOOD AFTERNOON · {research.asOfDate}</p><h1>综合投资研究看板</h1><p>从全市场筛选到单只证券研究，再到新闻、公告、日报和决策日志，所有证据回到同一张桌面。</p></div><button className="primary-action" onClick={() => navigate("screening")}>打开全市场筛选 <span>→</span></button></div>
          <div className="metric-strip">
            <article><span>全市场筛选</span><strong>{research.coverage.screeningTotal.toLocaleString("zh-CN")}</strong><small>投资范围内证券</small></article>
            <article><span>优先研究池</span><strong>{(research.coverage.priorityCounts.P0 + research.coverage.priorityCounts.P1).toLocaleString("zh-CN")}</strong><small>P0 {research.coverage.priorityCounts.P0} · P1 {research.coverage.priorityCounts.P1}</small></article>
            <article><span>行情覆盖</span><strong>{research.coverage.quoteCount.toLocaleString("zh-CN")}</strong><small>截至 {research.asOfDate}</small></article>
            <article><span>研究内容</span><strong>{research.content.length}</strong><small>日报、新闻、公告与报告</small></article>
          </div>
          <div className="overview-grid">
            <article className="surface-card top-candidates"><div className="card-heading"><div><span>PRIORITY QUEUE</span><h2>优先研究候选</h2></div><button onClick={() => navigate("screening")}>查看全部</button></div><div className="candidate-list">{topCandidates.map((item) => <button key={item.securityCode} onClick={() => openSecurity(item)}><span className="candidate-rank">{String(item.rank).padStart(2, "0")}</span><span className="candidate-name"><strong>{item.securityName}</strong><small>{item.securityCode}</small></span><span className={`priority-chip ${priorityClass(item.priority)}`}>{item.priority}</span><span className="candidate-score">{(item.score * 100).toFixed(1)}</span></button>)}</div></article>
            <article className="surface-card portfolio-glance"><div className="card-heading"><div><span>MY POSITIONS</span><h2>持仓快照</h2></div><button onClick={() => navigate("portfolio")}>管理持仓</button></div><div className="glance-summary"><div><span>投入本金</span><strong>{money.format(portfolio.executedPrincipalBeforeFees)}</strong></div><div><span>费用前现金</span><strong>{money.format(portfolio.estimatedRemainingCashBeforeFees)}</strong></div></div><div className="mini-position-list">{portfolio.positions.map((position) => <button key={position.securityCode} onClick={() => openHolding(position)}><span><strong>{position.securityName}</strong><small>{position.securityCode}</small></span><em>{position.quantity.toLocaleString("zh-CN")} 份</em><b>{position.latestPrice === null ? "待行情" : money.format(position.latestPrice)}</b></button>)}</div></article>
            <article className="surface-card intelligence-feed"><div className="card-heading"><div><span>LATEST INTELLIGENCE</span><h2>最新研究内容</h2></div><button onClick={() => navigate("content")}>进入内容中心</button></div><div className="feed-list">{research.content.slice(0, 6).map((item) => <button key={item.id} onClick={() => setSelectedContent(item)}><span className={`content-type type-${item.type}`}>{contentLabels[item.type]}</span><div><strong>{item.title}</strong><small>{item.date.slice(0, 10)} · {item.source}</small></div></button>)}</div></article>
            <article className="surface-card research-map"><div className="card-heading"><div><span>RESEARCH MAP</span><h2>研究链路</h2></div></div><div className="research-steps"><button onClick={() => navigate("screening")}><span>1</span><div><strong>筛选</strong><small>排序、过滤、建立候选池</small></div><em>{research.coverage.screeningTotal}</em></button><button onClick={() => openSecurity(topCandidates[0])}><span>2</span><div><strong>证券研究</strong><small>行情、财务、评分与内容</small></div><em>联动</em></button><button onClick={() => navigate("content")}><span>3</span><div><strong>证据阅读</strong><small>日报、新闻、公告、报告</small></div><em>{research.content.length}</em></button><button onClick={() => navigate("journal")}><span>4</span><div><strong>形成决策</strong><small>记录理由、反证与复盘日</small></div><em>{logs.entries.length}</em></button></div></article>
          </div>
        </section>}

        {view === "screening" && <section className="dashboard-view">
          <div className="section-title-row"><div><p className="section-eyebrow">MARKET RESEARCH QUEUE · {research.screeningRunId.slice(0, 8)}</p><h1>全市场筛选结果</h1><p>查看 {research.coverage.screeningTotal.toLocaleString("zh-CN")} 条筛选记录，按优先级、资格、评分、市值和估值自由过滤与排序。</p></div><div className="priority-legend"><span className="priority-p0">P0 {research.coverage.priorityCounts.P0}</span><span className="priority-p1">P1 {research.coverage.priorityCounts.P1}</span><span className="priority-p2">P2 {research.coverage.priorityCounts.P2}</span><span className="priority-reject">Reject {research.coverage.priorityCounts.Reject}</span></div></div>
          <div className="filter-panel">
            <label className="wide-filter"><span>搜索</span><input value={screenQuery} onChange={(event) => { setScreenQuery(event.target.value); setPage(1); }} placeholder="代码或简称" /></label>
            <label><span>优先级</span><select value={priority} onChange={(event) => { setPriority(event.target.value); setPage(1); }}><option value="all">全部</option><option value="P0">P0</option><option value="P1">P1</option><option value="P2">P2</option><option value="Reject">Reject</option></select></label>
            <label><span>筛选资格</span><select value={eligibility} onChange={(event) => { setEligibility(event.target.value); setPage(1); }}><option value="all">全部</option><option value="eligible">通过初筛</option><option value="ineligible">未通过</option></select></label>
            <label><span>市值区间</span><select value={capFilter} onChange={(event) => { setCapFilter(event.target.value); setPage(1); }}><option value="all">不限</option><option value="mega">1000 亿以上</option><option value="large">300–1000 亿</option><option value="small">300 亿以下</option></select></label>
            <label><span>排序</span><select value={sortKey} onChange={(event) => { setSortKey(event.target.value); setPage(1); }}><option value="rank">综合排名</option><option value="score">评分从高到低</option><option value="pe">PE 从低到高</option><option value="marketCap">市值从大到小</option><option value="margin">净利率从高到低</option></select></label>
            <label className="score-filter"><span>最低评分 <b>{minScore}</b></span><input type="range" min="0" max="100" value={minScore} onChange={(event) => { setMinScore(Number(event.target.value)); setPage(1); }} /></label>
            <button className="reset-filter" onClick={() => { setScreenQuery(""); setPriority("all"); setEligibility("all"); setMinScore(0); setCapFilter("all"); setSortKey("rank"); setPage(1); }}>重置</button>
          </div>
          <div className="surface-card screener-card"><div className="table-toolbar"><div><strong>{filteredSecurities.length.toLocaleString("zh-CN")}</strong><span> 条符合条件</span></div><small>点击任意证券查看行情、财务和评分</small></div><div className="data-table-wrap"><table className="research-table"><thead><tr><th>排名</th><th>证券</th><th>优先级</th><th>评分</th><th>PE TTM</th><th>总市值</th><th>净利率</th><th>现金流利润率</th><th>状态</th></tr></thead><tbody>{visibleSecurities.map((item) => <tr key={item.securityCode} onClick={() => openSecurity(item)} tabIndex={0} onKeyDown={(event) => { if (event.key === "Enter") openSecurity(item); }}><td className="rank-cell">{item.rank}</td><td><div className="table-security"><strong>{item.securityName}</strong><small>{item.securityCode}</small></div></td><td><span className={`priority-chip ${priorityClass(item.priority)}`}>{item.priority}</span></td><td><div className="score-cell"><strong>{(item.score * 100).toFixed(1)}</strong><span><i style={{ width: `${item.score * 100}%` }} /></span></div></td><td>{item.peTtm === null ? "—" : item.peTtm.toFixed(2)}</td><td>{formatMarketCap(item.marketCap)}</td><td>{formatPercent(item.netProfitMargin)}</td><td>{formatPercent(item.operatingCashFlowMargin)}</td><td><span className={`eligibility ${item.eligible ? "passed" : "failed"}`}><i />{item.eligible ? "通过" : "未通过"}</span></td></tr>)}</tbody></table></div><div className="pagination"><span>第 {page} / {totalPages} 页</span><div><button disabled={page === 1} onClick={() => setPage((current) => Math.max(1, current - 1))}>上一页</button><button disabled={page === totalPages} onClick={() => setPage((current) => Math.min(totalPages, current + 1))}>下一页</button></div></div></div>
        </section>}

        {view === "portfolio" && <section className="dashboard-view">
          <div className="section-title-row"><div><p className="section-eyebrow">PORTFOLIO · {portfolio.asOfDate}</p><h1>我的持仓</h1><p>持仓来自固定 GitHub 数据源；行情缺失时保持空值，不做推算。</p></div><span className="source-badge"><i />GitHub 已同步</span></div>
          <div className="metric-strip portfolio-metrics"><article><span>已执行本金</span><strong>{money.format(portfolio.executedPrincipalBeforeFees)}</strong><small>费用前</small></article><article><span>预估剩余现金</span><strong>{money.format(portfolio.estimatedRemainingCashBeforeFees)}</strong><small>费用前</small></article><article><span>持仓证券</span><strong>{portfolio.positionCount}</strong><small>全部为用户报告成交</small></article><article><span>已覆盖市值</span><strong>{money.format(coveredValue)}</strong><small>{portfolio.quoteCoverage.available}/{portfolio.quoteCoverage.total} 有行情</small></article></div>
          <div className="surface-card screener-card"><div className="card-heading holdings-heading"><div><span>CURRENT POSITIONS</span><h2>当前持仓明细</h2></div><small>点击证券进入研究详情</small></div><div className="data-table-wrap"><table className="research-table portfolio-table"><thead><tr><th>证券</th><th>数量</th><th>平均成本</th><th>成本金额</th><th>最新价</th><th>估算市值</th><th>行情状态</th></tr></thead><tbody>{portfolio.positions.map((position) => <tr key={position.securityCode} onClick={() => openHolding(position)}><td><div className="table-security"><strong>{position.securityName}</strong><small>{position.securityCode}</small></div></td><td>{position.quantity.toLocaleString("zh-CN")}</td><td>{money.format(position.averageCost)}</td><td>{money.format(position.costBasis)}</td><td>{position.latestPrice === null ? "—" : money.format(position.latestPrice)}</td><td>{position.marketValue === null ? "不可估算" : money.format(position.marketValue)}</td><td><span className={`eligibility ${position.quoteStatus === "available" ? "passed" : "pending"}`}><i />{position.quoteStatus === "available" ? position.quoteDate : "行情待落库"}</span></td></tr>)}</tbody></table></div><p className="table-footnote">费用、税费与缺失行情未计入；页面不会据此生成收益率或收益曲线。</p></div>
        </section>}

        {view === "content" && <section className="dashboard-view">
          <div className="section-title-row"><div><p className="section-eyebrow">RESEARCH LIBRARY · 2026-08-09</p><h1>日报、新闻、公告与研究报告</h1><p>在一个内容库里搜索、筛选和阅读已归档材料，并随时回到原始来源。</p></div><div className="content-counts"><span>新闻 {research.coverage.newsCount}</span><span>公告 {research.coverage.announcementCount}</span><span>日报 {research.coverage.dailyCount}</span><span>报告 {research.coverage.reportCount}</span></div></div>
          <div className="content-toolbar"><div className="segmented-control">{(Object.keys(contentLabels) as Array<ResearchContentType | "all">).map((key) => <button key={key} className={contentType === key ? "active" : ""} onClick={() => setContentType(key)}>{contentLabels[key]}</button>)}</div><label><span>⌕</span><input value={contentQuery} onChange={(event) => setContentQuery(event.target.value)} placeholder="搜索标题、摘要或证券" /></label></div>
          <div className="content-layout"><div className="content-list">{filteredContent.length ? filteredContent.map((item) => <button className="content-card" key={item.id} onClick={() => setSelectedContent(item)}><div><span className={`content-type type-${item.type}`}>{contentLabels[item.type]}</span><small>{item.date.slice(0, 10)}</small></div><h2>{item.title}</h2><p>{item.summary}</p><footer><span>{item.source}</span>{item.securityName && <em>{item.securityName}</em>}<b>阅读 →</b></footer></button>) : <EmptyState title="没有匹配内容" copy="调整内容类型或搜索关键词后重试。" />}</div><aside className="library-status"><div><span>内容覆盖</span><strong>{research.content.length}</strong><small>条可阅读记录</small></div><dl><div><dt>机构研报</dt><dd className="missing-text">尚未独立入库</dd></div><div><dt>内部研究</dt><dd>{research.coverage.reportCount} 份</dd></div><div><dt>原文链接</dt><dd>新闻与公告保留</dd></div><div><dt>事实边界</dt><dd>不做利好/利空判断</dd></div></dl><p>“研究报告”当前展示仓库内的内部研究与数据验证文档；机构研报数据源接入前会保持明确缺失状态。</p></aside></div>
        </section>}

        {view === "journal" && <section className="dashboard-view">
          <div className="section-title-row"><div><p className="section-eyebrow">PRIVATE DECISION JOURNAL</p><h1>决策日志</h1><p>把结论、证据、风险、信心与计划复盘日放进同一条可编辑记录。</p></div><span className={`source-badge ${logs.available ? "" : "warning"}`}><i />{logs.available ? `私有数据库已连接 · ${logs.entries.length} 条` : logs.message}</span></div>
          <div className="surface-card journal-composer"><div className="card-heading"><div><span>NEW ENTRY</span><h2>记录新决策</h2></div><small>仅 GitHub 白名单账户可见</small></div><DecisionForm disabled={!logs.available} asOfDate={portfolio.asOfDate} /></div>
          <div className="journal-list">{logs.entries.length ? logs.entries.map((entry) => <article className="journal-card-light" key={entry.id}><header><div><span className="decision-tag">{decisionLabels[entry.decisionType] ?? entry.decisionType}</span><small>{entry.decisionDate}</small><h2>{entry.securityName}<em>{entry.securityCode}</em></h2></div><span className="journal-status">{statusLabels[entry.status] ?? entry.status}</span></header><div className="journal-evidence"><div><span>核心理由</span><p>{entry.reason}</p></div><div><span>证据</span><p>{entry.evidence}</p></div><div><span>风险与反证</span><p>{entry.risks}</p></div></div><footer><span>信心：{confidenceLabels[entry.confidence] ?? entry.confidence}</span><span>计划复盘：{entry.reviewDate ?? "未设置"}</span><span>更新：{entry.updatedAt.slice(0, 10)}</span></footer><details><summary>编辑这条记录</summary><DecisionForm entry={entry} asOfDate={portfolio.asOfDate} /></details></article>) : <EmptyState title={logs.available ? "还没有决策日志" : "数据库暂不可用"} copy={logs.available ? "从筛选结果或证券详情中选择一只证券，记录第一条研究决策。" : logs.message ?? "数据库暂时不可用"} />}</div>
        </section>}

        {view === "data" && <section className="dashboard-view">
          <div className="section-title-row"><div><p className="section-eyebrow">DATA LINEAGE & COVERAGE</p><h1>数据状态</h1><p>每一个模块都说明来源、时间、记录数量和缺失边界。</p></div><span className="source-badge"><i />构建于 {new Date(research.generatedAt).toLocaleString("zh-CN", { hour12: false })}</span></div>
          <div className="source-grid">{research.sources.map((source) => <article className="source-card" key={source.label}><header><span className={`source-status status-${source.status}`}>{source.status === "ready" ? "完整" : source.status === "partial" ? "部分" : "缺失"}</span><strong>{source.label}</strong></header><dl><div><dt>记录</dt><dd>{source.records.toLocaleString("zh-CN")}</dd></div><div><dt>截至</dt><dd>{source.asOfDate}</dd></div><div className="source-path"><dt>来源</dt><dd>{source.path}</dd></div></dl></article>)}</div>
          <div className="data-notes"><article><span>01</span><div><h2>不会补造行情</h2><p>单只证券没有已落库行情时显示空值；不会用成本价、估值或其他近似值冒充市场价格。</p></div></article><article><span>02</span><div><h2>机构研报仍是缺口</h2><p>当前“研究报告”来自内部研究与数据验证文档。机构研报为 0 条，后续需新增独立采集与许可边界。</p></div></article><article><span>03</span><div><h2>筛选不是交易指令</h2><p>评分只用于排列研究优先级，必须结合证券详情、反证和决策日志继续研究。</p></div></article></div>
        </section>}
      </div>
    </section>
    {selectedSecurity && <SecurityDrawer key={selectedSecurity.securityCode} security={selectedSecurity} research={research} onClose={() => setSelectedSecurity(null)} onOpenJournal={() => { setSelectedSecurity(null); navigate("journal"); }} />}
    {selectedContent && <ContentReader item={selectedContent} onClose={() => setSelectedContent(null)} />}
  </main>;
}
