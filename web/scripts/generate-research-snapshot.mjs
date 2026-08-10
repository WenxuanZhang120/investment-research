import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(webRoot, "..");
const outputFile = path.join(webRoot, "src", "generated", "research-snapshot.json");
const financialOutputFile = path.join(webRoot, "src", "generated", "security-financials.json");
const fixedBranch = "codex/github-connector-small-files";
const screeningRunId = "b8e8a88ce7d140110835";
const screeningSourceFile = `data/derived/runs/screening/2026/08/07/${screeningRunId}/market_research_queue.jsonl`;

if (process.env.VERCEL_GIT_COMMIT_REF && process.env.VERCEL_GIT_COMMIT_REF !== fixedBranch) {
  throw new Error(`Research builds are restricted to the fixed GitHub branch: ${fixedBranch}`);
}

function normalizeCode(value) {
  const code = String(value ?? "").trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ)$/.test(code)) return code;
  if (/^\d{6}$/.test(code)) return `${code}.${code.startsWith("6") || code.startsWith("5") ? "SH" : "SZ"}`;
  return code;
}

function finiteOrNull(value) {
  const numeric = Number(value);
  return Number.isFinite(numeric) ? numeric : null;
}

function compactText(value, limit = 1200) {
  const text = String(value ?? "").replace(/\s+/g, " ").trim();
  return text.length > limit ? `${text.slice(0, limit).trim()}…` : text;
}

async function readJsonLines(relativePath) {
  try {
    return (await readFile(path.join(repoRoot, relativePath), "utf8"))
      .split(/\r?\n/)
      .filter(Boolean)
      .flatMap((line) => {
        try { return [JSON.parse(line)]; } catch { return []; }
      });
  } catch {
    return [];
  }
}

async function findFiles(directory, acceptedNames) {
  const matches = [];
  let entries = [];
  try { entries = await readdir(directory, { withFileTypes: true }); } catch { return matches; }
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory() && entry.name !== "pipeline-runs") matches.push(...(await findFiles(absolute, acceptedNames)));
    if (entry.isFile() && (acceptedNames.has(entry.name) || [...acceptedNames].some((name) => name.startsWith(".") && entry.name.endsWith(name)))) matches.push(absolute);
  }
  return matches;
}

const screeningRows = await readJsonLines(screeningSourceFile);
const securities = screeningRows.map((row) => ({
  securityCode: normalizeCode(row.security_code),
  securityName: String(row.security_name ?? ""),
  rank: Number(row.rank),
  priority: row.priority,
  score: Number(row.score),
  eligible: Boolean(row.eligible),
  eligibilityReasons: Array.isArray(row.eligibility_reasons) ? row.eligibility_reasons.map(String) : [],
  marketCap: finiteOrNull(row.market_cap),
  peTtm: finiteOrNull(row.pe_ttm),
  netProfitMargin: finiteOrNull(row.net_profit_margin),
  operatingCashFlowMargin: finiteOrNull(row.operating_cash_flow_margin),
  financialPeriodEnd: row.financial_period_end ? String(row.financial_period_end) : null,
  scoreComponents: {
    marketCap: finiteOrNull(row.score_components?.market_cap),
    peTtm: finiteOrNull(row.score_components?.pe_ttm),
    netProfitMargin: finiteOrNull(row.score_components?.net_profit_margin),
    operatingCashFlowMargin: finiteOrNull(row.score_components?.operating_cash_flow_margin),
  },
}));
const screenedCodes = new Set(securities.map((item) => item.securityCode));

const quotes = {};
const marketFiles = await findFiles(path.join(repoRoot, "data", "normalized", "runs"), new Set(["market_bars_daily.jsonl"]));
for (const marketFile of marketFiles) {
  const rows = (await readFile(marketFile, "utf8")).split(/\r?\n/).filter(Boolean);
  for (const line of rows) {
    let row;
    try { row = JSON.parse(line); } catch { continue; }
    const code = normalizeCode(row.security_code);
    const close = finiteOrNull(row.close);
    const tradeDate = String(row.trade_date ?? "");
    if (!screenedCodes.has(code) || close === null || !tradeDate) continue;
    if (!quotes[code] || tradeDate > quotes[code].tradeDate) {
      quotes[code] = { close, tradeDate, currency: String(row.currency ?? "CNY"), source: String(row.source ?? "iwencai") };
    }
  }
}

const financialBundles = [
  {
    periodEnd: "2024-12-31", reportType: "2024FY",
    root: "data/normalized/runs/iwencai/2026/08/09/e061123b4d1164cda321",
    metrics: null,
  },
  {
    periodEnd: "2025-12-31", reportType: "2025FY",
    root: "data/normalized/runs/iwencai/2026/08/08/085444945234b7d8d601",
    metrics: "data/derived/runs/iwencai/2026/08/08/c851f3cdfa64076c512b/financial_metrics_2025-12-31.jsonl",
  },
  {
    periodEnd: "2026-03-31", reportType: "2026Q1",
    root: "data/normalized/runs/iwencai/2026/08/08/958d1944b9726944ca39",
    metrics: "data/derived/runs/iwencai/2026/08/08/588bb40581c5187e678c/financial_metrics_2026-03-31.jsonl",
  },
];
const financials = {};
const periodMap = new Map();
const factFileNames = ["balance_sheet", "cash_flow_statement", "income_statement"];

for (const bundle of financialBundles) {
  const reports = await readJsonLines(`${bundle.root}/financial_reports.jsonl`);
  const reportByCode = new Map(reports.map((row) => [normalizeCode(row.security_code), row]));
  for (const statement of factFileNames) {
    const rows = await readJsonLines(`${bundle.root}/financial_facts_${bundle.periodEnd}_${statement}.jsonl`);
    for (const row of rows) {
      const code = normalizeCode(row.security_code);
      if (!screenedCodes.has(code)) continue;
      const key = `${code}|${bundle.periodEnd}`;
      if (!periodMap.has(key)) periodMap.set(key, { facts: {}, metrics: {} });
      periodMap.get(key).facts[row.canonical_field_name] = row.value_status === "present" ? finiteOrNull(row.value) : null;
    }
  }
  if (bundle.metrics) {
    for (const row of await readJsonLines(bundle.metrics)) {
      const code = normalizeCode(row.security_code);
      if (!screenedCodes.has(code)) continue;
      const key = `${code}|${bundle.periodEnd}`;
      if (!periodMap.has(key)) periodMap.set(key, { facts: {}, metrics: {} });
      periodMap.get(key).metrics[row.metric_name] = row.calculation_status === "calculated" ? finiteOrNull(row.value) : null;
    }
  }
  for (const code of screenedCodes) {
    const values = periodMap.get(`${code}|${bundle.periodEnd}`);
    if (!values) continue;
    const report = reportByCode.get(code);
    const period = {
      periodEnd: bundle.periodEnd,
      reportType: bundle.reportType,
      reportLabel: String(report?.report_period_label ?? bundle.reportType),
      filingDate: report?.filing_date ? String(report.filing_date) : null,
      facts: values.facts,
      metrics: values.metrics,
    };
    if (!financials[code]) financials[code] = [];
    financials[code].push(period);
  }
}

const content = [];
const seenContent = new Set();
const newsFiles = await findFiles(path.join(repoRoot, "data", "normalized", "runs"), new Set(["news_items.jsonl"]));
for (const file of newsFiles) {
  for (const row of await readJsonLines(path.relative(repoRoot, file))) {
    const id = String(row.news_id ?? `${row.title}|${row.published_at}`);
    if (seenContent.has(id)) continue;
    seenContent.add(id);
    content.push({
      id: `news-${id}`, type: "news", title: String(row.title ?? "未命名新闻"),
      date: String(row.published_at ?? row.available_from ?? ""), source: String(row.publisher ?? row.source ?? "同花顺问财"),
      url: row.url ? String(row.url) : null, summary: compactText(row.summary), content: null,
      securityCode: row.security_code ? normalizeCode(row.security_code) : null,
      securityName: row.security_name ? String(row.security_name) : null,
      tags: [String(row.event_type ?? "news")],
    });
  }
}

const eventFiles = await findFiles(path.join(repoRoot, "data", "normalized", "runs"), new Set(["events.jsonl"]));
for (const file of eventFiles) {
  for (const row of await readJsonLines(path.relative(repoRoot, file))) {
    const id = String(row.event_id ?? `${row.title}|${row.published_at}`);
    if (seenContent.has(id)) continue;
    seenContent.add(id);
    const sourceCode = row.security_code ?? row.source_security_code;
    content.push({
      id: `announcement-${id}`, type: "announcement", title: String(row.title ?? "未命名公告"),
      date: String(row.published_at ?? row.available_from ?? ""), source: String(row.source ?? "交易所公告"),
      url: row.url ? String(row.url) : null, summary: compactText(row.summary), content: null,
      securityCode: sourceCode ? normalizeCode(sourceCode) : null,
      securityName: row.security_name ? String(row.security_name) : null,
      tags: [String(row.event_type ?? "announcement"), ...(Array.isArray(row.classification_keywords) ? row.classification_keywords.map(String) : [])],
    });
  }
}

const markdownFiles = await findFiles(path.join(repoRoot, "reports", "daily"), new Set([".md"]));
for (const file of markdownFiles) {
  const relative = path.relative(repoRoot, file);
  const markdown = await readFile(file, "utf8");
  const title = markdown.match(/^#\s+(.+)$/m)?.[1]?.trim() ?? path.basename(file, ".md");
  const date = relative.match(/20\d{2}[-/]\d{2}[-/]\d{2}/)?.[0]?.replaceAll("/", "-") ?? "";
  const isDaily = /financial-news|announcement-events|财经新闻日报|公告事件日报/.test(`${relative}\n${title}`);
  content.push({
    id: `document-${relative}`, type: isDaily ? "daily" : "report", title, date,
    source: isDaily ? "每日自动采集" : "内部研究与数据验证", url: null,
    summary: compactText(markdown.replace(/^#.+$/m, ""), 320), content: markdown.slice(0, 18000),
    securityCode: null, securityName: null, tags: isDaily ? ["日报"] : ["研究报告", "数据验证"],
  });
}

content.sort((a, b) => String(b.date).localeCompare(String(a.date)) || a.title.localeCompare(b.title, "zh-CN"));
const priorityCounts = { P0: 0, P1: 0, P2: 0, Reject: 0 };
for (const security of securities) priorityCounts[security.priority] = (priorityCounts[security.priority] ?? 0) + 1;
const coverage = {
  screeningTotal: securities.length,
  priorityCounts,
  quoteCount: Object.keys(quotes).length,
  financialSecurityCount: Object.keys(financials).length,
  newsCount: content.filter((item) => item.type === "news").length,
  announcementCount: content.filter((item) => item.type === "announcement").length,
  dailyCount: content.filter((item) => item.type === "daily").length,
  reportCount: content.filter((item) => item.type === "report").length,
  institutionalResearchCount: 0,
};

const snapshot = {
  schemaVersion: "1.0.0", generatedAt: new Date().toISOString(), asOfDate: "2026-08-07",
  screeningRunId, screeningSourceFile, securities, quotes, content, coverage,
  sources: [
    { label: "全市场筛选", path: screeningSourceFile, asOfDate: "2026-08-07", records: securities.length, status: "ready" },
    { label: "收盘行情", path: "data/normalized/runs/**/market_bars_daily.jsonl", asOfDate: "2026-08-07", records: coverage.quoteCount, status: "partial" },
    { label: "财务与财报", path: "data/normalized + data/derived/financial_metrics", asOfDate: "2026-03-31", records: coverage.financialSecurityCount, status: "ready" },
    { label: "新闻与公告", path: "data/normalized/runs/**/{news_items,events}.jsonl", asOfDate: "2026-08-09", records: coverage.newsCount + coverage.announcementCount, status: "partial" },
    { label: "日报与内部研究", path: "reports/daily/**/*.md", asOfDate: "2026-08-09", records: coverage.dailyCount + coverage.reportCount, status: "ready" },
    { label: "机构研究报告", path: "尚未入库", asOfDate: "—", records: 0, status: "missing" },
  ],
};

await mkdir(path.dirname(outputFile), { recursive: true });
await writeFile(outputFile, `${JSON.stringify(snapshot)}\n`, "utf8");
await writeFile(financialOutputFile, `${JSON.stringify(financials)}\n`, "utf8");
console.log(`Research snapshot generated: ${securities.length} securities, ${content.length} content items.`);
