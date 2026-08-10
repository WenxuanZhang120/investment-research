import fallbackResearchJson from "@/generated/research-snapshot.json";
import fallbackPortfolioJson from "@/generated/portfolio-snapshot.json";
import type {
  QuoteSnapshot,
  ResearchContentItem,
  ResearchPriority,
  ResearchSnapshot,
  ScreeningSecurity,
} from "@/lib/research-types";
import type { PortfolioSnapshot } from "@/lib/types";

const repository = process.env.GITHUB_DATA_REPOSITORY ?? "WenxuanZhang120/investment-research";
const dataBranch = "main";
const refreshSeconds = 5 * 60;
const fallbackResearch = fallbackResearchJson as unknown as ResearchSnapshot;
const fallbackPortfolio = fallbackPortfolioJson as unknown as PortfolioSnapshot;

type GitHubTreeEntry = { path: string; type: "blob" | "tree"; sha: string; size?: number };
type GitHubTree = { tree: GitHubTreeEntry[]; truncated: boolean };
type JsonRecord = Record<string, unknown>;

function githubHeaders() {
  const headers: Record<string, string> = {
    Accept: "application/vnd.github+json",
    "User-Agent": "invescope-research-dashboard",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  if (process.env.GITHUB_DATA_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_DATA_TOKEN}`;
  return headers;
}

async function fetchJson<T>(url: string): Promise<T> {
  const response = await fetch(url, {
    headers: githubHeaders(),
    next: { revalidate: refreshSeconds },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`GitHub API ${response.status}`);
  return response.json() as Promise<T>;
}

function encodePath(value: string) {
  return value.split("/").map(encodeURIComponent).join("/");
}

async function fetchRepositoryText(path: string) {
  const url = `https://raw.githubusercontent.com/${repository}/refs/heads/${dataBranch}/${encodePath(path)}`;
  const response = await fetch(url, {
    next: { revalidate: refreshSeconds },
    signal: AbortSignal.timeout(30_000),
  });
  if (!response.ok) throw new Error(`GitHub raw ${response.status}`);
  return response.text();
}

function normalizeCode(value: unknown) {
  const code = String(value ?? "").trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ)$/.test(code)) return code;
  if (/^\d{6}$/.test(code)) return `${code}.${code.startsWith("6") || code.startsWith("5") ? "SH" : "SZ"}`;
  return code;
}

function finiteOrNull(value: unknown) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function compactText(value: unknown, limit = 1200) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit).trim()}…` : text;
}

function safeHttpUrl(value: unknown) {
  if (!value) return null;
  try {
    const url = new URL(String(value));
    return url.protocol === "https:" || url.protocol === "http:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function parseJsonLines(source: string) {
  return source.split(/\r?\n/).filter(Boolean).flatMap((line) => {
    try { return [JSON.parse(line) as JsonRecord]; } catch { return []; }
  });
}

function pathDate(path: string) {
  const match = path.match(/\/(20\d{2})\/(\d{2})\/(\d{2})\//);
  return match ? `${match[1]}-${match[2]}-${match[3]}` : null;
}

function latestDatedFiles(entries: GitHubTreeEntry[], predicate: (path: string) => boolean) {
  const candidates = entries.filter((entry) => entry.type === "blob" && predicate(entry.path))
    .map((entry) => ({ entry, date: pathDate(entry.path) }))
    .filter((item): item is { entry: GitHubTreeEntry; date: string } => Boolean(item.date));
  const latestDate = candidates.reduce<string | null>((latest, item) => !latest || item.date > latest ? item.date : latest, null);
  return latestDate ? candidates.filter((item) => item.date === latestDate).map((item) => item.entry.path) : [];
}

function screeningFromRows(rows: JsonRecord[]): ScreeningSecurity[] {
  return rows.map((row) => {
    const scoreComponents = (row.score_components ?? {}) as JsonRecord;
    const rawPriority = String(row.priority ?? "Reject");
    const priority: ResearchPriority = rawPriority === "P0" || rawPriority === "P1" || rawPriority === "P2" ? rawPriority : "Reject";
    return {
      securityCode: normalizeCode(row.security_code),
      securityName: String(row.security_name ?? row.security_code ?? "未命名证券"),
      rank: Number(row.rank),
      priority,
      score: Number(row.score),
      eligible: Boolean(row.eligible),
      eligibilityReasons: Array.isArray(row.eligibility_reasons) ? row.eligibility_reasons.map(String) : [],
      marketCap: finiteOrNull(row.market_cap),
      peTtm: finiteOrNull(row.pe_ttm),
      netProfitMargin: finiteOrNull(row.net_profit_margin),
      operatingCashFlowMargin: finiteOrNull(row.operating_cash_flow_margin),
      financialPeriodEnd: row.financial_period_end ? String(row.financial_period_end) : null,
      scoreComponents: {
        marketCap: finiteOrNull(scoreComponents.market_cap),
        peTtm: finiteOrNull(scoreComponents.pe_ttm),
        netProfitMargin: finiteOrNull(scoreComponents.net_profit_margin),
        operatingCashFlowMargin: finiteOrNull(scoreComponents.operating_cash_flow_margin),
      },
    };
  }).filter((item) => item.securityCode && item.securityName && Number.isFinite(item.score));
}

function quoteFromRow(row: JsonRecord): { code: string; quote: QuoteSnapshot } | null {
  const code = normalizeCode(row.security_code ?? row.code ?? row.fund_code);
  const close = finiteOrNull(row.close ?? row.latest_price ?? row.price ?? row.unit_net_value);
  const tradeDate = String(row.trade_date ?? row.as_of_date ?? row.nav_date ?? "");
  if (!code || close === null || !tradeDate) return null;
  return {
    code,
    quote: { close, tradeDate, currency: String(row.currency ?? "CNY"), source: String(row.source ?? "repository") },
  };
}

function contentFromNews(row: JsonRecord): ResearchContentItem {
  const id = String(row.news_id ?? `${row.title}|${row.published_at}`);
  return {
    id: `news-${id}`,
    type: "news",
    title: String(row.title ?? "未命名新闻"),
    date: String(row.published_at ?? row.available_from ?? ""),
    source: String(row.publisher ?? row.source ?? "同花顺问财"),
    url: safeHttpUrl(row.url),
    summary: compactText(row.summary),
    content: null,
    securityCode: row.security_code ? normalizeCode(row.security_code) : null,
    securityName: row.security_name ? String(row.security_name) : null,
    tags: [String(row.event_type ?? "news")],
  };
}

function contentFromEvent(row: JsonRecord): ResearchContentItem {
  const id = String(row.event_id ?? `${row.title}|${row.published_at}`);
  const sourceCode = row.security_code ?? row.source_security_code;
  return {
    id: `announcement-${id}`,
    type: "announcement",
    title: String(row.title ?? "未命名公告"),
    date: String(row.published_at ?? row.available_from ?? ""),
    source: String(row.source ?? "交易所公告"),
    url: safeHttpUrl(row.url),
    summary: compactText(row.summary),
    content: null,
    securityCode: sourceCode ? normalizeCode(sourceCode) : null,
    securityName: row.security_name ? String(row.security_name) : null,
    tags: [String(row.event_type ?? "announcement"), ...(Array.isArray(row.classification_keywords) ? row.classification_keywords.map(String) : [])],
  };
}

function contentFromMarkdown(path: string, markdown: string): ResearchContentItem {
  const title = markdown.match(/^#\s+(.+)$/m)?.[1]?.trim() ?? path.split("/").at(-1)?.replace(/\.md$/, "") ?? "未命名日报";
  const date = pathDate(path) ?? "";
  return {
    id: `document-${path}`,
    type: "daily",
    title,
    date,
    source: "每日自动采集",
    url: null,
    summary: compactText(markdown.replace(/^#.+$/m, ""), 320),
    content: markdown.slice(0, 18_000),
    securityCode: null,
    securityName: null,
    tags: ["日报"],
  };
}

async function loadLiveResearch(): Promise<ResearchSnapshot> {
  const tree = await fetchJson<GitHubTree>(`https://api.github.com/repos/${repository}/git/trees/${dataBranch}?recursive=1`);
  const entries = tree.tree;
  const screeningFiles = latestDatedFiles(entries, (path) => /data\/derived\/runs\/screening\/.+\/market_research_queue\.jsonl$/.test(path));
  const marketFiles = latestDatedFiles(entries, (path) => /data\/normalized\/runs\/.+\/(market_bars_daily|etf_snapshots)\.jsonl$/.test(path));
  const newsFiles = latestDatedFiles(entries, (path) => /data\/normalized\/runs\/.+\/news_items\.jsonl$/.test(path));
  const eventFiles = latestDatedFiles(entries, (path) => /data\/normalized\/runs\/.+\/events\.jsonl$/.test(path));
  const dailyFiles = latestDatedFiles(entries, (path) => /reports\/daily\/monitoring\/(news|events)\/.+\.md$/.test(path));

  const selectedPaths = [...new Set([
    ...screeningFiles.slice(-1),
    ...marketFiles,
    ...newsFiles,
    ...eventFiles,
    ...dailyFiles,
  ])];
  if (!selectedPaths.length) throw new Error("No readable research files in latest branch");

  const fetched = await Promise.allSettled(selectedPaths.map(async (path) => [path, await fetchRepositoryText(path)] as const));
  const files = new Map(fetched.flatMap((result) => result.status === "fulfilled" ? [result.value] : []));
  if (!files.size) throw new Error("GitHub research files unavailable");

  const latestScreeningPath = screeningFiles.slice(-1)[0];
  const liveScreening = latestScreeningPath && files.has(latestScreeningPath)
    ? screeningFromRows(parseJsonLines(files.get(latestScreeningPath)!))
    : [];
  const screeningComplete = liveScreening.length >= Math.floor(fallbackResearch.securities.length * 0.8);
  const securities = screeningComplete ? liveScreening : fallbackResearch.securities;

  const quotes: Record<string, QuoteSnapshot> = { ...fallbackResearch.quotes };
  for (const path of marketFiles) {
    const source = files.get(path);
    if (!source) continue;
    for (const row of parseJsonLines(source)) {
      const value = quoteFromRow(row);
      if (!value) continue;
      if (!quotes[value.code] || value.quote.tradeDate > quotes[value.code].tradeDate) quotes[value.code] = value.quote;
    }
  }

  const contentById = new Map(fallbackResearch.content.map((item) => [item.id, item]));
  for (const path of newsFiles) {
    const source = files.get(path);
    if (source) for (const row of parseJsonLines(source)) {
      const item = contentFromNews(row);
      contentById.set(item.id, item);
    }
  }
  for (const path of eventFiles) {
    const source = files.get(path);
    if (source) for (const row of parseJsonLines(source)) {
      const item = contentFromEvent(row);
      contentById.set(item.id, item);
    }
  }
  for (const path of dailyFiles) {
    const source = files.get(path);
    if (source) {
      const item = contentFromMarkdown(path, source);
      contentById.set(item.id, item);
    }
  }
  const content = [...contentById.values()].sort((left, right) => String(right.date).localeCompare(String(left.date)) || left.title.localeCompare(right.title, "zh-CN"));

  const priorityCounts: Record<ResearchPriority, number> = { P0: 0, P1: 0, P2: 0, Reject: 0 };
  for (const security of securities) priorityCounts[security.priority] = (priorityCounts[security.priority] ?? 0) + 1;
  const latestQuoteDate = Object.values(quotes).reduce<string | null>((latest, quote) => !latest || quote.tradeDate > latest ? quote.tradeDate : latest, null);
  const latestContentDate = content.reduce<string | null>((latest, item) => {
    const date = item.date.slice(0, 10);
    return /^20\d{2}-\d{2}-\d{2}$/.test(date) && (!latest || date > latest) ? date : latest;
  }, null);
  const latestDate = [pathDate(latestScreeningPath ?? ""), latestQuoteDate, latestContentDate]
    .filter((value): value is string => Boolean(value)).sort().at(-1) ?? null;
  const screeningDate = pathDate(latestScreeningPath ?? "") ?? fallbackResearch.sources.find((source) => source.label === "全市场筛选")?.asOfDate ?? fallbackResearch.asOfDate;

  const coverage = {
    ...fallbackResearch.coverage,
    screeningTotal: securities.length,
    priorityCounts,
    quoteCount: Object.keys(quotes).length,
    newsCount: content.filter((item) => item.type === "news").length,
    announcementCount: content.filter((item) => item.type === "announcement").length,
    dailyCount: content.filter((item) => item.type === "daily").length,
    reportCount: content.filter((item) => item.type === "report").length,
  };
  const sources = fallbackResearch.sources.map((source) => {
    if (source.label === "全市场筛选" && latestScreeningPath && screeningComplete) return { ...source, path: `${dataBranch}:${latestScreeningPath}`, asOfDate: screeningDate, records: securities.length };
    if (source.label === "收盘行情" && marketFiles.some((path) => files.has(path))) return { ...source, path: `${dataBranch}:data/normalized/runs/**/{market_bars_daily,etf_snapshots}.jsonl`, asOfDate: latestQuoteDate ?? source.asOfDate, records: coverage.quoteCount };
    if (source.label === "新闻与公告" && [...newsFiles, ...eventFiles].some((path) => files.has(path))) return { ...source, path: `${dataBranch}:data/normalized/runs/**/{news_items,events}.jsonl`, asOfDate: latestContentDate ?? source.asOfDate, records: coverage.newsCount + coverage.announcementCount };
    if (source.label === "日报与内部研究" && dailyFiles.some((path) => files.has(path))) return { ...source, path: `${dataBranch}:reports/daily/monitoring/**/*.md`, asOfDate: latestContentDate ?? source.asOfDate, records: coverage.dailyCount + coverage.reportCount };
    return source;
  });

  return {
    ...fallbackResearch,
    generatedAt: new Date().toISOString(),
    asOfDate: latestQuoteDate && latestQuoteDate > fallbackResearch.asOfDate ? latestQuoteDate : fallbackResearch.asOfDate,
    screeningRunId: latestScreeningPath?.split("/").at(-2) ?? fallbackResearch.screeningRunId,
    screeningSourceFile: latestScreeningPath ?? fallbackResearch.screeningSourceFile,
    securities,
    quotes,
    content,
    coverage,
    sources,
    liveData: {
      status: "live",
      branch: dataBranch,
      latestDate,
      checkedAt: new Date().toISOString(),
      message: tree.truncated ? "GitHub 文件树较大，已读取其中可见的最新研究数据。" : null,
    },
  };
}

export async function getResearchSnapshot(): Promise<ResearchSnapshot> {
  try {
    return await loadLiveResearch();
  } catch (error) {
    console.error("Live GitHub research data unavailable", error);
    return {
      ...fallbackResearch,
      liveData: {
        status: "fallback",
        branch: dataBranch,
        latestDate: fallbackResearch.sources.find((source) => source.label === "新闻与公告")?.asOfDate ?? fallbackResearch.asOfDate,
        checkedAt: new Date().toISOString(),
        message: "GitHub main 暂时不可用，当前显示上一份已验证快照。",
      },
    };
  }
}

function parseCsvLine(line: string) {
  const values: string[] = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"' && line[index + 1] === '"') {
      value += '"';
      index += 1;
    } else if (character === '"') {
      quoted = !quoted;
    } else if (character === "," && !quoted) {
      values.push(value);
      value = "";
    } else {
      value += character;
    }
  }
  values.push(value);
  return values;
}

function parseCsv(source: string) {
  const [headerLine, ...lines] = source.trim().split(/\r?\n/);
  if (!headerLine) return [];
  const headers = parseCsvLine(headerLine);
  return lines.filter(Boolean).map((line) => {
    const values = parseCsvLine(line);
    return Object.fromEntries(headers.map((header, index) => [header, values[index] ?? ""]));
  });
}

function roundCurrency(value: number) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

async function loadLivePortfolio(): Promise<PortfolioSnapshot> {
  const [holdingsSource, executionSource] = await Promise.all([
    fetchRepositoryText("portfolio/holdings.csv"),
    fetchRepositoryText("portfolio/execution_status.json"),
  ]);
  const holdings = parseCsv(holdingsSource);
  const execution = JSON.parse(executionSource) as JsonRecord;
  if (!holdings.length) throw new Error("GitHub main portfolio is empty");

  const positions = holdings.map((holding) => {
    const quantity = Number(holding.quantity);
    const averageCost = Number(holding.average_cost);
    if (!holding.as_of_date || !holding.security_code || !holding.security_name || !Number.isFinite(quantity) || !Number.isFinite(averageCost)) {
      throw new Error("GitHub main portfolio contains an invalid holding");
    }
    return {
      asOfDate: holding.as_of_date,
      securityCode: normalizeCode(holding.security_code),
      securityName: holding.security_name,
      quantity,
      averageCost,
      currency: holding.currency || "CNY",
      costBasis: roundCurrency(quantity * averageCost),
      targetWeight: holding.target_weight ? Number(holding.target_weight) : null,
      notes: holding.notes ?? "",
      latestPrice: null,
      quoteDate: null,
      marketValue: null,
      quoteSource: null,
      quoteSourceFile: null,
      quoteStatus: "missing" as const,
    };
  });

  return {
    schemaVersion: String(execution.schema_version ?? "1.0.0"),
    generatedAt: new Date().toISOString(),
    fixedGitHubSource: {
      repository,
      branch: dataBranch,
      directory: "portfolio",
      holdingsFile: "portfolio/holdings.csv",
      executionFile: "portfolio/execution_status.json",
    },
    asOfDate: String(execution.as_of_date ?? positions[0].asOfDate),
    source: String(execution.source ?? "repository"),
    positionCount: positions.length,
    quoteCoverage: { available: 0, total: positions.length },
    executedPrincipalBeforeFees: Number(execution.executed_principal_before_fees ?? 0),
    estimatedRemainingCashBeforeFees: Number(execution.estimated_remaining_cash_before_fees ?? 0),
    positions,
    liveData: {
      status: "live",
      branch: dataBranch,
      checkedAt: new Date().toISOString(),
      message: null,
    },
  };
}

export async function getPortfolioSnapshot(): Promise<PortfolioSnapshot> {
  try {
    return await loadLivePortfolio();
  } catch (error) {
    console.error("Live GitHub portfolio unavailable", error);
    return {
      ...fallbackPortfolio,
      fixedGitHubSource: {
        repository,
        branch: dataBranch,
        directory: "portfolio",
        holdingsFile: "portfolio/holdings.csv",
        executionFile: "portfolio/execution_status.json",
      },
      liveData: {
        status: "fallback",
        branch: dataBranch,
        checkedAt: new Date().toISOString(),
        message: "GitHub main 持仓暂时不可用，当前显示上一份已验证快照。",
      },
    };
  }
}

export function refreshPortfolioQuotes(portfolio: PortfolioSnapshot, research: ResearchSnapshot): PortfolioSnapshot {
  let available = 0;
  const positions = portfolio.positions.map((position) => {
    const quote = research.quotes[position.securityCode];
    if (!quote || (position.quoteDate && quote.tradeDate <= position.quoteDate)) {
      if (position.quoteStatus === "available") available += 1;
      return position;
    }
    available += 1;
    return {
      ...position,
      latestPrice: quote.close,
      quoteDate: quote.tradeDate,
      marketValue: Math.round((position.quantity * quote.close + Number.EPSILON) * 100) / 100,
      quoteSource: quote.source,
      quoteSourceFile: `GitHub ${research.liveData?.branch ?? dataBranch}`,
      quoteStatus: "available" as const,
    };
  });
  return { ...portfolio, positions, quoteCoverage: { available, total: positions.length } };
}
