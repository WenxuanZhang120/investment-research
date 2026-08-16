import { useEffect, useMemo, useRef, useState } from "react";

type DataStatus = "ready" | "empty" | "missing" | "partial" | string;

type DatasetSummary = {
  status: DataStatus;
  recordCount: number;
  asOfDate?: string | null;
};

type PublicIndex = {
  schemaVersion: number;
  generatedAt: string | null;
  status: DataStatus;
  datasets: Record<string, DatasetSummary>;
  fileCount: number;
};

type PipelineStep = {
  stage: string | null;
  stepId: string | null;
  status: string | null;
  exitCode: number | null;
  errorType: string | null;
  startedAt: string | null;
  finishedAt: string | null;
};

type PipelinePayload = {
  schemaVersion: number;
  status: DataStatus;
  artifactAvailable: boolean;
  run: null | {
    runId: string | null;
    pipelineVersion: string | null;
    status: string | null;
    startedAt: string | null;
    finishedAt: string | null;
    stepCount: number | null;
    steps: PipelineStep[];
    readiness: {
      status: string | null;
      plannedStepCount: number | null;
      incompleteJobCount: number | null;
      researchStatus: string | null;
      screeningStatus: string | null;
      monitoringMatchedSnapshotCount: number | null;
    };
  };
};

type MarketShardDescriptor = {
  name: string;
  path: string;
  recordCount: number;
};

type MarketSummary = {
  schemaVersion: number;
  status: DataStatus;
  asOfDate: string | null;
  bundleId: string | null;
  screeningVersion: string | null;
  purpose: string | null;
  recordCount: number;
  eligibleCount: number;
  rejectCount: number;
  priorityCounts: Record<string, number>;
  shards: MarketShardDescriptor[];
};

type MarketRecord = {
  asOfDate?: string;
  securityCode?: string;
  securityName?: string;
  eligible?: boolean;
  eligibilityReasons?: string[];
  priority?: string;
  rank?: number | null;
  score?: number | null;
  scoreComponents?: Record<string, number>;
  marketCap?: number | null;
  peTtm?: number | null;
  netProfitMargin?: number | null;
  operatingCashFlowMargin?: number | null;
  financialPeriodEnd?: string;
  financialAvailableFrom?: string;
};

type MarketShard = {
  schemaVersion: number;
  status: DataStatus;
  shard: string;
  recordCount: number;
  records: MarketRecord[];
};

type EtfRecord = {
  etfCode?: string;
  etfName?: string;
  exchange?: string;
  asOfDate?: string;
  trackedIndex?: string | null;
  fundType?: string | null;
  price?: number | null;
  changePct?: number | null;
  fundSize?: number | null;
  premiumDiscountRate?: number | null;
  trackingError?: number | null;
};

type EtfIndex = {
  schemaVersion: number;
  status: DataStatus;
  asOfDate: string | null;
  recordCount: number;
  records: EtfRecord[];
};

type CompanySummary = {
  securityCode: string;
  securityName: string | null;
  priority: string | null;
  rank: number | null;
  hasMarket: boolean;
  financialReportCount: number;
  financialFactCount: number;
  newsCount: number;
  eventCount: number;
  detailShard: string;
};

type CompaniesIndex = {
  schemaVersion: number;
  status: DataStatus;
  recordCount: number;
  companies: CompanySummary[];
};

type FinancialReport = {
  securityCode?: string;
  securityName?: string;
  periodEnd?: string;
  reportType?: string;
  reportPeriodLabel?: string;
  filingDate?: string;
  availableFrom?: string;
  factCount?: number;
  presentFactCount?: number;
  missingFactCount?: number;
};

type FinancialFact = {
  securityCode?: string;
  securityName?: string;
  periodEnd?: string;
  reportType?: string;
  reportPeriodLabel?: string;
  filingDate?: string;
  availableFrom?: string;
  canonicalFieldName?: string;
  statementType?: string;
  value?: string | number | null;
  unit?: string | null;
  valueNature?: string;
  valueStatus?: string;
};

type CompanyDetail = {
  securityCode: string;
  securityName: string | null;
  market: MarketRecord | null;
  financialReports: FinancialReport[];
  financialFacts: FinancialFact[];
  newsIds: string[];
  eventIds: string[];
};

type CompanyDetailShard = {
  schemaVersion: number;
  status: DataStatus;
  shard: string;
  recordCount: number;
  companies: CompanyDetail[];
};

type ContentRecord = {
  newsId?: string;
  eventId?: string;
  eventType?: string;
  publishedAt?: string;
  availableFrom?: string;
  publisher?: string | null;
  securityCode?: string | null;
  sourceSecurityCode?: string | null;
  securityName?: string | null;
  title?: string;
  summary?: string;
  url?: string;
  classificationKeywords?: string[];
};

type ReportRecord = {
  kind: string;
  title: string;
  asOfDate: string | null;
  path: string;
  sha256: string;
  bytes: number;
  recordCount: number | null;
  status: DataStatus;
};

type ContentIndex = {
  schemaVersion: number;
  status: DataStatus;
  domains: Record<string, DatasetSummary>;
  news: ContentRecord[];
  events: ContentRecord[];
  reports: ReportRecord[];
};

type ProvenanceSource = {
  domain: string;
  path: string;
  sha256: string;
  bytes: number;
  bundleId: string | null;
  runId: string | null;
  asOfDate: string | null;
  fetchedAt: string | null;
  recordCount: number | null;
};

type ProvenanceIndex = {
  schemaVersion: number;
  status: DataStatus;
  generatedAt: string | null;
  sourceCount: number;
  sources: ProvenanceSource[];
};

type SortMode = "rank-asc" | "score-desc" | "marketCap-desc" | "peTtm-asc" | "name-asc";
type ContentCategory = "all" | "news" | "events" | "reports";

const EMPTY_DATASET: DatasetSummary = { status: "missing", recordCount: 0, asOfDate: null };

const EMPTY_INDEX: PublicIndex = {
  schemaVersion: 1,
  generatedAt: null,
  status: "missing",
  datasets: {},
  fileCount: 0,
};

const EMPTY_PIPELINE: PipelinePayload = { schemaVersion: 1, status: "missing", artifactAvailable: false, run: null };

const EMPTY_MARKET: MarketSummary = {
  schemaVersion: 1,
  status: "missing",
  asOfDate: null,
  bundleId: null,
  screeningVersion: null,
  purpose: null,
  recordCount: 0,
  eligibleCount: 0,
  rejectCount: 0,
  priorityCounts: {},
  shards: [],
};

const EMPTY_COMPANIES: CompaniesIndex = {
  schemaVersion: 1,
  status: "missing",
  recordCount: 0,
  companies: [],
};

const EMPTY_ETF: EtfIndex = {
  schemaVersion: 1,
  status: "missing",
  asOfDate: null,
  recordCount: 0,
  records: [],
};

const EMPTY_CONTENT: ContentIndex = {
  schemaVersion: 1,
  status: "missing",
  domains: {},
  news: [],
  events: [],
  reports: [],
};

const EMPTY_PROVENANCE: ProvenanceIndex = {
  schemaVersion: 1,
  status: "missing",
  generatedAt: null,
  sourceCount: 0,
  sources: [],
};

const modules = [
  { key: "A", href: "#collection", label: "采集状态" },
  { key: "B", href: "#market", label: "市场与 ETF" },
  { key: "C", href: "#companies", label: "公司详情" },
  { key: "D", href: "#research", label: "研究成果" },
] as const;

const priorityOrder = ["P0", "P1", "P2", "Reject"];
const priorityLabels: Record<string, string> = {
  P0: "P0 深度研究",
  P1: "P1 重点确认",
  P2: "P2 持续观察",
  Reject: "未通过筛选",
};

const datasetLabels: Record<string, string> = {
  pipeline: "流水线",
  market: "市场筛选",
  etf: "ETF 快照",
  financial: "财务事实",
  news: "财经新闻",
  events: "公告事件",
  reports: "研究报告",
};

const statusLabels: Record<string, string> = {
  ready: "就绪",
  empty: "合法零结果",
  missing: "未接入",
  partial: "部分就绪",
  succeeded: "执行成功",
  failed: "执行失败",
  waiting_for_complete_input: "等待完整输入",
  waiting_for_inputs: "等待研究输入",
  waiting_for_full_market_and_metrics: "等待完整市场与指标",
};

const financialFieldLabels: Record<string, string> = {
  revenue: "营业收入",
  net_profit: "净利润",
  net_profit_attributable_to_parent: "归母净利润",
  operating_cash_flow: "经营现金流",
  total_assets: "资产总计",
  total_liabilities: "负债合计",
  roe: "ROE",
};

function statusLabel(status: string | null | undefined) {
  if (!status) return "未接入";
  return statusLabels[status] ?? status.replaceAll("_", " ");
}

function statusClass(status: string | null | undefined) {
  if (status === "ready" || status === "succeeded") return "good";
  if (status === "empty") return "neutral";
  if (status === "missing" || !status) return "muted";
  return "warn";
}

function formatNumber(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN").format(value);
}

function formatCompact(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(value);
}

function formatDecimal(value: number | null | undefined, digits = 2) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return value.toLocaleString("zh-CN", { maximumFractionDigits: digits });
}

function formatDate(value: string | null | undefined, includeTime = false) {
  if (!value) return "未接入";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.valueOf())) return value.slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    ...(includeTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  }).format(parsed);
}

function repositoryUrl(path: string) {
  const safePath = path.split("/").map(encodeURIComponent).join("/");
  return `https://github.com/WenxuanZhang120/investment-research/blob/main/${safePath}`;
}

async function shardForCode(code: string) {
  const bytes = new TextEncoder().encode(code);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return new Uint8Array(digest)[0].toString(16).padStart(2, "0")[0];
}

function App() {
  const [rootIndex, setRootIndex] = useState<PublicIndex>(EMPTY_INDEX);
  const [pipeline, setPipeline] = useState<PipelinePayload>(EMPTY_PIPELINE);
  const [marketSummary, setMarketSummary] = useState<MarketSummary>(EMPTY_MARKET);
  const [etfIndex, setEtfIndex] = useState<EtfIndex>(EMPTY_ETF);
  const [companiesIndex, setCompaniesIndex] = useState<CompaniesIndex>(EMPTY_COMPANIES);
  const [content, setContent] = useState<ContentIndex>(EMPTY_CONTENT);
  const [provenance, setProvenance] = useState<ProvenanceIndex>(EMPTY_PROVENANCE);
  const [snapshotLoad, setSnapshotLoad] = useState<"loading" | "ready" | "partial" | "missing">("loading");

  const [marketRecords, setMarketRecords] = useState<MarketRecord[]>([]);
  const [marketLoading, setMarketLoading] = useState(false);
  const [marketError, setMarketError] = useState<string | null>(null);
  const [marketLoaded, setMarketLoaded] = useState(false);
  const [marketSearch, setMarketSearch] = useState("");
  const [priorityFilter, setPriorityFilter] = useState("all");
  const [sortMode, setSortMode] = useState<SortMode>("rank-asc");
  const [marketPage, setMarketPage] = useState(1);

  const [companySearch, setCompanySearch] = useState("");
  const [selectedCompany, setSelectedCompany] = useState<CompanySummary | null>(null);
  const [companyDetail, setCompanyDetail] = useState<CompanyDetail | null>(null);
  const [companyLoading, setCompanyLoading] = useState(false);
  const [companyError, setCompanyError] = useState<string | null>(null);
  const [contentCategory, setContentCategory] = useState<ContentCategory>("all");
  const detailRequest = useRef(0);
  const closeButtonRef = useRef<HTMLButtonElement>(null);

  const dataBase = `${import.meta.env.BASE_URL}data/`;

  async function loadJson<T>(relativePath: string, signal?: AbortSignal): Promise<T> {
    const response = await fetch(`${dataBase}${relativePath}`, {
      cache: "no-store",
      signal,
    });
    if (!response.ok) throw new Error(`${relativePath}: HTTP ${response.status}`);
    return response.json() as Promise<T>;
  }

  useEffect(() => {
    const controller = new AbortController();

    async function loadInitialSnapshot() {
      const results = await Promise.allSettled([
        loadJson<PublicIndex>("index.json", controller.signal),
        loadJson<PipelinePayload>("status/latest.json", controller.signal),
        loadJson<MarketSummary>("market/summary.json", controller.signal),
        loadJson<EtfIndex>("etf/index.json", controller.signal),
        loadJson<CompaniesIndex>("companies/index.json", controller.signal),
        loadJson<ContentIndex>("content/index.json", controller.signal),
        loadJson<ProvenanceIndex>("provenance/index.json", controller.signal),
      ]);
      if (controller.signal.aborted) return;

      let loaded = 0;
      const [rootResult, pipelineResult, marketResult, etfResult, companiesResult, contentResult, provenanceResult] = results;
      if (rootResult.status === "fulfilled") { setRootIndex(rootResult.value); loaded += 1; }
      if (pipelineResult.status === "fulfilled") { setPipeline(pipelineResult.value); loaded += 1; }
      if (marketResult.status === "fulfilled") { setMarketSummary(marketResult.value); loaded += 1; }
      if (etfResult.status === "fulfilled") { setEtfIndex(etfResult.value); loaded += 1; }
      if (companiesResult.status === "fulfilled") { setCompaniesIndex(companiesResult.value); loaded += 1; }
      if (contentResult.status === "fulfilled") { setContent(contentResult.value); loaded += 1; }
      if (provenanceResult.status === "fulfilled") { setProvenance(provenanceResult.value); loaded += 1; }
      setSnapshotLoad(loaded === results.length ? "ready" : loaded > 0 ? "partial" : "missing");
    }

    void loadInitialSnapshot();
    return () => controller.abort();
  }, []);

  useEffect(() => {
    if (!selectedCompany) return;
    closeButtonRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSelectedCompany(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [selectedCompany]);

  const latestDataDate = useMemo(() => {
    const dates = Object.values(rootIndex.datasets)
      .map((dataset) => dataset.asOfDate)
      .filter((value): value is string => Boolean(value));
    return dates.sort().at(-1) ?? null;
  }, [rootIndex.datasets]);

  const credibility = useMemo(() => {
    if (snapshotLoad === "loading") return { label: "正在核验", level: "pending", note: "正在读取公开快照与来源索引。" };
    if (rootIndex.status === "ready" && provenance.status === "ready" && pipeline.status === "ready") {
      return { label: "已验证公开快照", level: "high", note: `${provenance.sourceCount} 个来源条目已经隐私白名单与哈希校验。` };
    }
    if (pipeline.status === "failed") {
      return { label: "来源快照可用·流水线失败", level: "limited", note: `状态工件${pipeline.artifactAvailable ? "已生成" : "未生成"}，本次发布状态为“${statusLabel(pipeline.status)}”。` };
    }
    if (provenance.status === "ready" && ["ready", "partial"].includes(rootIndex.status)) {
      return { label: "来源已验证·数据待完整", level: "medium", note: `${provenance.sourceCount} 个来源已验证，但流水线发布状态为“${statusLabel(pipeline.status)}”，不视为完整快照。` };
    }
    if (snapshotLoad === "partial") return { label: "部分数据就绪", level: "medium", note: "有公开数据文件未成功加载，页面仅展示已获取部分。" };
    return { label: "公开快照未接入", level: "limited", note: "当前未显示任何演示数量，请等待下一次验证发布。" };
  }, [pipeline.artifactAvailable, pipeline.status, provenance.sourceCount, provenance.status, rootIndex.status, snapshotLoad]);

  const priorityRows = useMemo(() => {
    const known = priorityOrder.filter((key) => key in marketSummary.priorityCounts);
    const extra = Object.keys(marketSummary.priorityCounts).filter((key) => !priorityOrder.includes(key)).sort();
    return [...known, ...extra].map((key) => ({
      key,
      count: marketSummary.priorityCounts[key] ?? 0,
      percent: marketSummary.recordCount > 0
        ? ((marketSummary.priorityCounts[key] ?? 0) / marketSummary.recordCount) * 100
        : 0,
    }));
  }, [marketSummary.priorityCounts, marketSummary.recordCount]);

  async function loadMarketQueue() {
    if (marketLoading || marketLoaded || marketSummary.shards.length === 0) return;
    setMarketLoading(true);
    setMarketError(null);
    try {
      const shards = await Promise.all(
        marketSummary.shards.map((descriptor) => loadJson<MarketShard>(descriptor.path)),
      );
      const records = shards.flatMap((shard) => shard.records);
      const expected = marketSummary.shards.reduce((sum, shard) => sum + shard.recordCount, 0);
      if (records.length !== expected || records.length !== marketSummary.recordCount) {
        throw new Error("分片记录数与摘要不一致");
      }
      setMarketRecords(records);
      setMarketLoaded(true);
    } catch {
      setMarketError("市场分片未能完整加载，已停止展示明细以避免误导。");
    } finally {
      setMarketLoading(false);
    }
  }

  const filteredMarket = useMemo(() => {
    const term = marketSearch.trim().toLocaleLowerCase("zh-CN");
    const filtered = marketRecords.filter((record) => {
      const matchesSearch = !term || `${record.securityName ?? ""} ${record.securityCode ?? ""}`.toLocaleLowerCase("zh-CN").includes(term);
      const matchesPriority = priorityFilter === "all" || record.priority === priorityFilter;
      return matchesSearch && matchesPriority;
    });
    const [sortKey, direction] = sortMode.split("-") as ["rank" | "score" | "marketCap" | "peTtm" | "name", "asc" | "desc"];
    return [...filtered].sort((a, b) => {
      const aValue = sortKey === "name" ? (a.securityName ?? a.securityCode ?? "") : a[sortKey];
      const bValue = sortKey === "name" ? (b.securityName ?? b.securityCode ?? "") : b[sortKey];
      if (aValue == null && bValue == null) return 0;
      if (aValue == null) return 1;
      if (bValue == null) return -1;
      const comparison = typeof aValue === "string"
        ? aValue.localeCompare(String(bValue), "zh-CN")
        : Number(aValue) - Number(bValue);
      return direction === "asc" ? comparison : -comparison;
    });
  }, [marketRecords, marketSearch, priorityFilter, sortMode]);

  const marketPageSize = 20;
  const marketPageCount = Math.max(1, Math.ceil(filteredMarket.length / marketPageSize));
  const marketPageRows = filteredMarket.slice((marketPage - 1) * marketPageSize, marketPage * marketPageSize);

  const visibleCompanies = useMemo(() => {
    const term = companySearch.trim().toLocaleLowerCase("zh-CN");
    const rows = companiesIndex.companies.filter((company) => {
      if (!term) return true;
      return `${company.securityName ?? ""} ${company.securityCode}`.toLocaleLowerCase("zh-CN").includes(term);
    });
    rows.sort((a, b) => {
      const aCoverage = a.financialFactCount + a.newsCount + a.eventCount;
      const bCoverage = b.financialFactCount + b.newsCount + b.eventCount;
      if (!term && aCoverage !== bCoverage) return bCoverage - aCoverage;
      return (a.rank ?? Number.MAX_SAFE_INTEGER) - (b.rank ?? Number.MAX_SAFE_INTEGER)
        || a.securityCode.localeCompare(b.securityCode);
    });
    return rows.slice(0, 12);
  }, [companiesIndex.companies, companySearch]);

  async function openCompany(company: CompanySummary) {
    const requestId = detailRequest.current + 1;
    detailRequest.current = requestId;
    setSelectedCompany(company);
    setCompanyDetail(null);
    setCompanyError(null);
    setCompanyLoading(true);
    try {
      const shard = await shardForCode(company.securityCode);
      const payload = await loadJson<CompanyDetailShard>(`companies/details-${shard}.json`);
      const detail = payload.companies.find((item) => item.securityCode === company.securityCode) ?? null;
      if (!detail) throw new Error("详情分片中未找到该证券");
      if (detailRequest.current === requestId) setCompanyDetail(detail);
    } catch {
      if (detailRequest.current === requestId) setCompanyError("该公司的详情分片未能完整加载。");
    } finally {
      if (detailRequest.current === requestId) setCompanyLoading(false);
    }
  }

  const companyRelatedContent = useMemo(() => {
    if (!companyDetail) return [];
    const newsIds = new Set(companyDetail.newsIds);
    const eventIds = new Set(companyDetail.eventIds);
    return [
      ...content.news.filter((item) => item.newsId && newsIds.has(item.newsId)),
      ...content.events.filter((item) => item.eventId && eventIds.has(item.eventId)),
    ];
  }, [companyDetail, content.events, content.news]);

  const contentFeed = useMemo(() => {
    const rows = [
      ...content.news.map((item) => ({
        id: `news-${item.newsId ?? item.title}`,
        category: "news" as const,
        title: item.title ?? "未命名新闻",
        date: item.publishedAt ?? item.availableFrom ?? null,
        source: item.publisher ?? "来源未标注",
        summary: item.summary ?? "",
        status: "ready",
        url: item.url ?? null,
        keywords: item.classificationKeywords ?? [],
      })),
      ...content.events.map((item) => ({
        id: `event-${item.eventId ?? item.title}`,
        category: "events" as const,
        title: item.title ?? "未命名公告事件",
        date: item.publishedAt ?? item.availableFrom ?? null,
        source: item.publisher ?? item.securityName ?? "公告事件",
        summary: item.summary ?? "",
        status: "ready",
        url: item.url ?? null,
        keywords: item.classificationKeywords ?? [],
      })),
      ...content.reports.map((item) => ({
        id: `report-${item.sha256}`,
        category: "reports" as const,
        title: item.title,
        date: item.asOfDate,
        source: `${item.kind.toUpperCase()} · ${item.recordCount ?? "—"} 条`,
        summary: `经验证的仓库研究成果 · ${formatCompact(item.bytes)}B`,
        status: item.status,
        url: repositoryUrl(item.path),
        keywords: [],
      })),
    ];
    rows.sort((a, b) => String(b.date ?? "").localeCompare(String(a.date ?? "")));
    return contentCategory === "all" ? rows : rows.filter((row) => row.category === contentCategory);
  }, [content, contentCategory]);

  const datasetCards = Object.keys(datasetLabels).map((key) => ({
    key,
    label: datasetLabels[key],
    value: rootIndex.datasets[key] ?? EMPTY_DATASET,
  }));

  const pipelineSucceeded = pipeline.run?.status === "succeeded";
  const readinessReady = pipeline.status === "ready";

  return (
    <div className="site-shell">
      <a className="skip-link" href="#main-content">跳至主要内容</a>

      <header className="topbar">
        <a className="brand" href="#top" aria-label="市场证据台首页">
          <span className="brand-mark" aria-hidden="true">ER</span>
          <span><strong>市场证据台</strong><small>EVIDENCE RESEARCH</small></span>
        </a>
        <nav className="module-nav" aria-label="主要模块">
          {modules.map((item) => <a href={item.href} key={item.key}><span>{item.key}</span>{item.label}</a>)}
        </nav>
        <div className="read-only-badge"><span aria-hidden="true" />公开·只读</div>
      </header>

      <main id="main-content">
        <section className="hero" id="top" aria-labelledby="hero-title">
          <div className="hero-copy">
            <p className="eyebrow">INVESTMENT RESEARCH / PUBLIC VIEW</p>
            <h1 id="hero-title">让每日采集，<br />成为可核验的研究入口。</h1>
            <p className="hero-intro">从原始响应、标准化到研究产出，将数据日期、覆盖度与来源放在同一条可追溯链路上。</p>
          </div>
          <aside className="snapshot-card" aria-label="当前数据快照">
            <div className="snapshot-topline"><span>DATA AS OF</span><span className={`source-pill ${credibility.level}`}>{snapshotLoad === "ready" ? "PUBLIC SNAPSHOT" : snapshotLoad.toUpperCase()}</span></div>
            <p className="snapshot-date">{latestDataDate ? formatDate(latestDataDate) : "未接入"}</p>
            <div className="credibility-row"><span className={`credibility-light ${credibility.level}`} aria-hidden="true" /><div><span>可信度</span><strong>{credibility.label}</strong></div></div>
            <p className="snapshot-note">{credibility.note}</p>
            <p className="generated-at">生成时间 {formatDate(rootIndex.generatedAt, true)}</p>
          </aside>
        </section>

        <section className="signal-strip" aria-label="公开数据覆盖摘要">
          <div><span>市场样本</span><strong>{formatNumber(rootIndex.datasets.market?.recordCount ?? 0)}</strong></div>
          <div><span>财务事实</span><strong>{formatNumber(rootIndex.datasets.financial?.recordCount ?? 0)}</strong></div>
          <div><span>内容条目</span><strong>{formatNumber((rootIndex.datasets.news?.recordCount ?? 0) + (rootIndex.datasets.events?.recordCount ?? 0) + (rootIndex.datasets.reports?.recordCount ?? 0))}</strong></div>
          <div><span>来源索引</span><strong>{formatNumber(provenance.sourceCount)}</strong></div>
          <p>只展示已通过公开发布门槛的字段；缺失与合法零结果均单独标记。</p>
        </section>

        <section className="product-section" id="collection" aria-labelledby="collection-title">
          <header className="section-heading">
            <div><p className="section-code">A / COLLECTION</p><h2 id="collection-title">采集与发布状态</h2></div>
            <p>流水线成功不等于研究数据已就绪。两个信号必须分开读取。</p>
          </header>

          <div className="status-duo">
            <article className="status-card">
              <span className="card-kicker">PIPELINE EXECUTION</span>
              <div className="status-title-row"><span className={`status-orb ${pipelineSucceeded ? "good" : "warn"}`} aria-hidden="true" /><h3>{statusLabel(pipeline.run?.status)}</h3></div>
              <p>{pipeline.run ? `${pipeline.run.stepCount ?? 0} 个步骤已完成，版本 ${pipeline.run.pipelineVersion ?? "未知"}。` : "尚未接入流水线记录。"}</p>
              <small>结束于 {formatDate(pipeline.run?.finishedAt, true)}</small>
            </article>
            <article className="status-card readiness-card">
              <span className="card-kicker">RESEARCH READINESS</span>
              <div className="status-title-row"><span className={`status-orb ${readinessReady ? "good" : "warn"}`} aria-hidden="true" /><h3>{statusLabel(pipeline.status)}</h3></div>
              <p>{pipeline.run ? `${pipeline.run.readiness.incompleteJobCount ?? 0} 项研究输入尚不完整；屏幕展示可用数据，不推断缺失部分。` : "尚未接入就绪度评估。"}</p>
              <small>筛选：{statusLabel(pipeline.run?.readiness.screeningStatus)}</small>
            </article>
          </div>

          <div className="collection-grid">
            <div className="dataset-grid" aria-label="公开数据集状态">
              {datasetCards.map(({ key, label, value }) => (
                <article className="dataset-card" key={key}>
                  <div><span>{label}</span><span className={`mini-status ${statusClass(value.status)}`}>{statusLabel(value.status)}</span></div>
                  <strong>{formatNumber(value.recordCount)}</strong>
                  <small>{value.asOfDate ? `数据日期 ${formatDate(value.asOfDate)}` : "暂无数据日期"}</small>
                </article>
              ))}
            </div>
            <div className="steps-panel">
              <div className="subheading"><span>最近步骤</span><small>{pipeline.run?.runId ? `RUN ${pipeline.run.runId.slice(0, 8)}` : "NO RUN"}</small></div>
              {pipeline.run?.steps.length ? (
                <ol className="step-list">
                  {pipeline.run.steps.map((step, index) => (
                    <li key={`${step.stepId}-${index}`}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{step.stepId ?? "未命名步骤"}</strong><small>{step.stage ?? "stage unknown"}</small></div><span className={`mini-status ${statusClass(step.status)}`}>{statusLabel(step.status)}</span></li>
                  ))}
                </ol>
              ) : <p className="empty-copy">暂无可公开的执行步骤。</p>}
            </div>
          </div>
        </section>

        <section className="product-section" id="market" aria-labelledby="market-title">
          <header className="section-heading">
            <div><p className="section-code">B / MARKET</p><h2 id="market-title">市场研究优先级</h2></div>
            <p>优先级是确定性研究排序，不是交易信号。</p>
          </header>

          <div className="market-overview">
            <article className="priority-panel">
              <div className="subheading"><span>优先级分布</span><small>{marketSummary.asOfDate ? formatDate(marketSummary.asOfDate) : "未接入"}</small></div>
              {priorityRows.length ? <div className="priority-chart">
                {priorityRows.map((item) => (
                  <div className="priority-row" key={item.key}>
                    <div><span>{priorityLabels[item.key] ?? item.key}</span><strong>{formatNumber(item.count)} <small>{item.percent.toFixed(1)}%</small></strong></div>
                    <progress
                      className={`priority-track priority-${item.key.toLowerCase()}`}
                      value={item.count}
                      max={marketSummary.recordCount || 1}
                      aria-label={`${priorityLabels[item.key] ?? item.key} ${item.count} 条`}
                    />
                  </div>
                ))}
              </div> : <p className="empty-copy">市场优先级摘要未接入，当前数量为 0。</p>}
            </article>
            <article className="coverage-panel">
              <span className="card-kicker">COVERAGE NOTES</span>
              <div className="coverage-stat"><strong>{formatNumber(marketSummary.eligibleCount)}</strong><span>通过筛选</span></div>
              <div className="coverage-stat"><strong>{formatNumber(marketSummary.rejectCount)}</strong><span>未通过筛选</span></div>
              {etfIndex.status === "ready" && etfIndex.recordCount > 0 ? (
                <div className="etf-preview"><div><strong>ETF 快照</strong><small>{formatDate(etfIndex.asOfDate)} · {formatNumber(etfIndex.recordCount)} 条</small></div>{etfIndex.records.slice(0, 3).map((record) => <div className="etf-row" key={`${record.etfCode}-${record.asOfDate}`}><span><strong>{record.etfName ?? "名称未接入"}</strong><small>{record.etfCode ?? "—"} · {record.trackedIndex ?? "跟踪指数未接入"}</small></span><span className="mono">{formatDecimal(record.price)}</span></div>)}</div>
              ) : (
                <div className="coverage-alert"><strong>尚无可发布 ETF 快照</strong><p>ETF 数据集状态为“{statusLabel(etfIndex.status)}”，记录数为 {formatNumber(etfIndex.recordCount)}。页面不会用市场队列或历史数值代替。</p></div>
              )}
            </article>
          </div>

          <div className="table-panel">
            <div className="table-intro">
              <div><span className="card-kicker">MARKET QUEUE / 16 SHARDS</span><h3>公开市场队列</h3></div>
              {!marketLoaded && <button className="primary-button" type="button" onClick={() => void loadMarketQueue()} disabled={marketLoading || marketSummary.shards.length === 0}>{marketLoading ? "正在校验 16 个分片…" : "加载市场明细"}</button>}
            </div>
            {marketError && <p className="error-banner" role="alert">{marketError}</p>}
            {marketLoaded ? (
              <>
                <div className="table-controls">
                  <label><span>搜索名称或代码</span><input type="search" value={marketSearch} onChange={(event) => { setMarketSearch(event.target.value); setMarketPage(1); }} placeholder="例如：平安银行 / 000001" /></label>
                  <label><span>优先级</span><select value={priorityFilter} onChange={(event) => { setPriorityFilter(event.target.value); setMarketPage(1); }}><option value="all">全部</option>{priorityOrder.map((priority) => <option value={priority} key={priority}>{priorityLabels[priority]}</option>)}</select></label>
                  <label><span>排序</span><select value={sortMode} onChange={(event) => { setSortMode(event.target.value as SortMode); setMarketPage(1); }}><option value="rank-asc">研究排名</option><option value="score-desc">综合得分</option><option value="marketCap-desc">市值</option><option value="peTtm-asc">PE TTM</option><option value="name-asc">名称</option></select></label>
                </div>
                <div className="table-scroll">
                  <table><caption className="sr-only">市场研究优先级明细</caption><thead><tr><th>排名</th><th>证券</th><th>优先级</th><th>得分</th><th>市值</th><th>PE TTM</th><th>财务期</th><th><span className="sr-only">操作</span></th></tr></thead>
                    <tbody>{marketPageRows.map((record) => {
                      const summary = companiesIndex.companies.find((company) => company.securityCode === record.securityCode);
                      return <tr key={record.securityCode}><td className="mono">{record.rank ?? "—"}</td><td><strong>{record.securityName ?? "未命名"}</strong><small>{record.securityCode ?? "—"}</small></td><td><span className={`priority-badge priority-${(record.priority ?? "unknown").toLowerCase()}`}>{record.priority ?? "—"}</span></td><td className="mono">{record.score == null ? "—" : (record.score * 100).toFixed(2)}</td><td className="mono">{formatCompact(record.marketCap)}</td><td className="mono">{formatDecimal(record.peTtm)}</td><td className="mono">{record.financialPeriodEnd ?? "—"}</td><td>{summary && <button className="text-button" type="button" onClick={() => void openCompany(summary)} aria-label={`查看 ${summary.securityName ?? summary.securityCode} 详情`}>查看</button>}</td></tr>;
                    })}</tbody></table>
                </div>
                <div className="pagination"><span>筛选结果 {formatNumber(filteredMarket.length)} 条</span><div><button type="button" onClick={() => setMarketPage((page) => Math.max(1, page - 1))} disabled={marketPage <= 1}>上一页</button><span>{marketPage} / {marketPageCount}</span><button type="button" onClick={() => setMarketPage((page) => Math.min(marketPageCount, page + 1))} disabled={marketPage >= marketPageCount}>下一页</button></div></div>
              </>
            ) : !marketError && <p className="load-note">明细不随首屏加载。点击后将同时校验 16 个小分片，只有完整时才展示表格。</p>}
          </div>
        </section>

        <section className="product-section" id="companies" aria-labelledby="companies-title">
          <header className="section-heading">
            <div><p className="section-code">C / COMPANIES</p><h2 id="companies-title">公司覆盖与详情</h2></div>
            <p>财务、市场与内容覆盖分别标记；未覆盖不代表数值为零。</p>
          </header>
          <div className="company-toolbar"><label><span>查找公司</span><input type="search" value={companySearch} onChange={(event) => setCompanySearch(event.target.value)} placeholder="输入证券名称或代码" /></label><p>已索引 {formatNumber(companiesIndex.recordCount)} 个证券主体</p></div>
          {visibleCompanies.length ? <div className="company-grid">
            {visibleCompanies.map((company) => (
              <button className="company-card" type="button" key={company.securityCode} onClick={() => void openCompany(company)}>
                <span className="company-card-top"><span className={`priority-badge priority-${(company.priority ?? "unknown").toLowerCase()}`}>{company.priority ?? "NO PRIORITY"}</span><span className="mono">{company.rank ? `#${company.rank}` : "—"}</span></span>
                <strong>{company.securityName ?? "名称未接入"}</strong><small>{company.securityCode}</small>
                <span className="coverage-tags"><span className={company.hasMarket ? "covered" : "missing"}>市场 {company.hasMarket ? "已覆盖" : "未覆盖"}</span><span className={company.financialFactCount > 0 ? "covered" : "missing"}>财务 {company.financialFactCount}</span><span className={company.newsCount + company.eventCount > 0 ? "covered" : "missing"}>内容 {company.newsCount + company.eventCount}</span></span>
                <span className="card-action">打开详情 <span aria-hidden="true">↗</span></span>
              </button>
            ))}
          </div> : <p className="empty-copy">未找到匹配公司，或公司索引未接入。</p>}
        </section>

        <section className="product-section" id="research" aria-labelledby="research-title">
          <header className="section-heading">
            <div><p className="section-code">D / RESEARCH</p><h2 id="research-title">新闻、公告与研究成果</h2></div>
            <p>外链指向原始发布页或 GitHub 中的可审计成果。</p>
          </header>
          <div className="content-domain-strip">
            {(["news", "events", "reports"] as const).map((domain) => {
              const domainState = content.domains[domain] ?? EMPTY_DATASET;
              return <div key={domain}><span>{datasetLabels[domain]}</span><strong>{formatNumber(domainState.recordCount)}</strong><small className={statusClass(domainState.status)}>{statusLabel(domainState.status)}</small></div>;
            })}
          </div>
          <div className="content-layout">
            <div className="content-column">
              <div className="filter-tabs" role="group" aria-label="内容分类筛选">
                {([['all', '全部'], ['news', '新闻'], ['events', '公告'], ['reports', '报告']] as const).map(([key, label]) => <button type="button" key={key} aria-pressed={contentCategory === key} onClick={() => setContentCategory(key)}>{label}</button>)}
              </div>
              {contentFeed.length ? <div className="content-feed">
                {contentFeed.slice(0, 18).map((item) => (
                  <article className="content-card" key={item.id}>
                    <div className="content-meta"><span className={`content-kind kind-${item.category}`}>{item.category === "news" ? "新闻" : item.category === "events" ? "公告" : "报告"}</span><time>{formatDate(item.date)}</time><span className={`mini-status ${statusClass(item.status)}`}>{statusLabel(item.status)}</span></div>
                    <h3>{item.title}</h3><p className="content-source">{item.source}</p>{item.summary && <p className="content-summary">{item.summary}</p>}
                    {item.keywords.length > 0 && <div className="keyword-row">{item.keywords.slice(0, 4).map((keyword) => <span key={keyword}>{keyword}</span>)}</div>}
                    {item.url ? <a href={item.url} target="_blank" rel="noreferrer">查看原始来源 <span aria-hidden="true">↗</span></a> : <span className="no-link">未提供公开外链</span>}
                  </article>
                ))}
              </div> : <p className="empty-copy">{contentCategory === "events" && content.domains.events?.status === "empty" ? "公告事件为合法零结果，并非加载失败。" : "该分类当前没有已发布内容。"}</p>}
            </div>
            <aside className="provenance-panel">
              <span className="card-kicker">PROVENANCE</span><h3>数据血缘</h3><p>来源清单、时间、条数与 SHA-256 由导出器生成，用于追溯每个公开快照。</p>
              <div className="provenance-summary"><strong>{formatNumber(provenance.sourceCount)}</strong><span>个已索引来源</span></div>
              <details><summary>展开全部来源</summary><ul>{provenance.sources.map((source) => <li key={`${source.domain}-${source.sha256}`}><div><span>{source.domain.toUpperCase()}</span><small>{formatDate(source.asOfDate)}</small></div><a href={repositoryUrl(source.path)} target="_blank" rel="noreferrer">{source.path}</a><code>{source.sha256.slice(0, 12)}… · {formatNumber(source.recordCount)} 条</code></li>)}</ul></details>
            </aside>
          </div>
        </section>
      </main>

      <footer><span>市场证据台 / PUBLIC RESEARCH ARCHIVE</span><p>仅用于数据整理与研究展示，不提供登录、持仓、交易或投资建议。</p></footer>

      {selectedCompany && (
        <div className="drawer-layer">
          <button className="drawer-backdrop" type="button" aria-label="关闭公司详情" onClick={() => setSelectedCompany(null)} />
          <aside className="company-drawer" role="dialog" aria-modal="true" aria-labelledby="company-drawer-title">
            <div className="drawer-header"><div><span className="card-kicker">COMPANY DETAIL</span><h2 id="company-drawer-title">{selectedCompany.securityName ?? "名称未接入"}</h2><p>{selectedCompany.securityCode}</p></div><button ref={closeButtonRef} type="button" onClick={() => setSelectedCompany(null)} aria-label="关闭公司详情">×</button></div>
            <div className="drawer-body">
              <div className="coverage-tags drawer-tags"><span className={selectedCompany.hasMarket ? "covered" : "missing"}>市场 {selectedCompany.hasMarket ? "已覆盖" : "未覆盖"}</span><span className={selectedCompany.financialFactCount > 0 ? "covered" : "missing"}>财务事实 {selectedCompany.financialFactCount}</span><span className={selectedCompany.newsCount > 0 ? "covered" : "missing"}>新闻 {selectedCompany.newsCount}</span><span className={selectedCompany.eventCount > 0 ? "covered" : "missing"}>公告 {selectedCompany.eventCount}</span></div>
              {companyLoading && <p className="drawer-loading" role="status">正在按代码哈希加载详情分片…</p>}
              {companyError && <p className="error-banner" role="alert">{companyError}</p>}
              {companyDetail && <>
                <section className="drawer-section"><h3>市场筛选摘要</h3>{companyDetail.market ? <div className="metric-grid"><div><span>优先级</span><strong>{companyDetail.market.priority ?? "—"}</strong></div><div><span>研究排名</span><strong>{companyDetail.market.rank ?? "—"}</strong></div><div><span>得分</span><strong>{companyDetail.market.score == null ? "—" : (companyDetail.market.score * 100).toFixed(2)}</strong></div><div><span>市值</span><strong>{formatCompact(companyDetail.market.marketCap)}</strong></div><div><span>PE TTM</span><strong>{formatDecimal(companyDetail.market.peTtm)}</strong></div><div><span>数据日期</span><strong>{companyDetail.market.asOfDate ?? "—"}</strong></div></div> : <p className="empty-copy">未覆盖市场筛选数据。</p>}</section>
                <section className="drawer-section"><h3>可用财务事实</h3>{companyDetail.financialFacts.length ? <div className="fact-list">{companyDetail.financialFacts.slice(0, 10).map((fact, index) => <div key={`${fact.canonicalFieldName}-${fact.periodEnd}-${index}`}><span>{financialFieldLabels[fact.canonicalFieldName ?? ""] ?? fact.canonicalFieldName ?? "未命名字段"}<small>{fact.periodEnd ?? fact.reportPeriodLabel ?? "—"}</small></span><strong>{typeof fact.value === "number" ? formatCompact(fact.value) : String(fact.value ?? "—")} <small>{fact.unit ?? ""}</small></strong></div>)}</div> : <p className="empty-copy">当前公开快照未覆盖该公司的财务事实。</p>}</section>
                <section className="drawer-section"><h3>定期报告覆盖</h3>{companyDetail.financialReports.length ? <ul className="report-list">{companyDetail.financialReports.slice(0, 6).map((report, index) => <li key={`${report.periodEnd}-${index}`}><span>{report.reportPeriodLabel ?? report.reportType ?? "定期报告"}</span><strong>{report.periodEnd ?? "—"}</strong><small>{report.presentFactCount ?? 0} / {report.factCount ?? 0} 事实可用</small></li>)}</ul> : <p className="empty-copy">未覆盖定期报告摘要。</p>}</section>
                <section className="drawer-section"><h3>相关公开内容</h3>{companyRelatedContent.length ? <ul className="related-list">{companyRelatedContent.slice(0, 6).map((item, index) => <li key={`${item.newsId ?? item.eventId}-${index}`}><a href={item.url} target="_blank" rel="noreferrer">{item.title ?? "未命名内容"}</a><small>{formatDate(item.publishedAt)} · {item.publisher ?? "来源未标注"}</small></li>)}</ul> : <p className="empty-copy">未发现与该代码关联的已发布新闻或公告。</p>}</section>
              </>}
            </div>
          </aside>
        </div>
      )}
    </div>
  );
}

export default App;
