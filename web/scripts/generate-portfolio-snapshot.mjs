import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDir, "..");
const repoRoot = path.resolve(webRoot, "..");
const portfolioRoot = path.join(repoRoot, "portfolio");
const marketRoot = path.join(repoRoot, "data", "normalized", "runs");
const outputFile = path.join(webRoot, "src", "generated", "portfolio-snapshot.json");
const buildBranch = "codex/github-connector-small-files";
const dataBranch = "main";

if (process.env.VERCEL_GIT_COMMIT_REF && process.env.VERCEL_GIT_COMMIT_REF !== buildBranch) {
  throw new Error(`Portfolio builds are restricted to the fixed GitHub branch: ${buildBranch}`);
}

function roundCurrency(value) {
  return Math.round((value + Number.EPSILON) * 100) / 100;
}

function parseCsvLine(line) {
  const values = [];
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

function parseCsv(source) {
  const [headerLine, ...lines] = source.trim().split(/\r?\n/);
  const headers = parseCsvLine(headerLine);
  return lines.filter(Boolean).map((line) =>
    Object.fromEntries(headers.map((header, index) => [header, parseCsvLine(line)[index] ?? ""])),
  );
}

async function findFiles(directory, acceptedNames) {
  const matches = [];
  let entries = [];
  try {
    entries = await readdir(directory, { withFileTypes: true });
  } catch {
    return matches;
  }
  for (const entry of entries) {
    const absolute = path.join(directory, entry.name);
    if (entry.isDirectory()) matches.push(...(await findFiles(absolute, acceptedNames)));
    if (entry.isFile() && acceptedNames.has(entry.name)) matches.push(absolute);
  }
  return matches;
}

function normalizeCode(value) {
  const code = String(value ?? "").trim().toUpperCase();
  if (/^\d{6}\.(SH|SZ)$/.test(code)) return code;
  if (/^\d{6}$/.test(code)) return `${code}.${code.startsWith("6") || code.startsWith("5") ? "SH" : "SZ"}`;
  return code;
}

const holdings = parseCsv(await readFile(path.join(portfolioRoot, "holdings.csv"), "utf8"));
const execution = JSON.parse(await readFile(path.join(portfolioRoot, "execution_status.json"), "utf8"));
const heldCodes = new Set(holdings.map((holding) => normalizeCode(holding.security_code)));
const latestQuotes = new Map();
const marketFiles = await findFiles(marketRoot, new Set(["market_bars_daily.jsonl", "etf_snapshots.jsonl"]));

for (const marketFile of marketFiles) {
  const lines = (await readFile(marketFile, "utf8")).split(/\r?\n/).filter(Boolean);
  for (const line of lines) {
    let record;
    try {
      record = JSON.parse(line);
    } catch {
      continue;
    }
    const code = normalizeCode(record.security_code ?? record.code ?? record.fund_code);
    const price = Number(record.close ?? record.latest_price ?? record.price ?? record.unit_net_value);
    const tradeDate = record.trade_date ?? record.as_of_date ?? record.nav_date ?? null;
    if (!heldCodes.has(code) || !Number.isFinite(price) || !tradeDate) continue;
    const previous = latestQuotes.get(code);
    if (!previous || String(tradeDate) > previous.tradeDate) {
      latestQuotes.set(code, {
        price,
        tradeDate: String(tradeDate),
        source: record.source ?? "normalized_repository_data",
        sourceFile: path.relative(repoRoot, marketFile),
      });
    }
  }
}

const positions = holdings.map((holding) => {
  const code = normalizeCode(holding.security_code);
  const quantity = Number(holding.quantity);
  const averageCost = Number(holding.average_cost);
  const quote = latestQuotes.get(code) ?? null;
  return {
    asOfDate: holding.as_of_date,
    securityCode: code,
    securityName: holding.security_name,
    quantity,
    averageCost,
    currency: holding.currency,
    costBasis: roundCurrency(quantity * averageCost),
    targetWeight: holding.target_weight ? Number(holding.target_weight) : null,
    notes: holding.notes,
    latestPrice: quote?.price ?? null,
    quoteDate: quote?.tradeDate ?? null,
    marketValue: quote ? roundCurrency(quantity * quote.price) : null,
    quoteSource: quote?.source ?? null,
    quoteSourceFile: quote?.sourceFile ?? null,
    quoteStatus: quote ? "available" : "missing",
  };
});

const quoteCount = positions.filter((position) => position.quoteStatus === "available").length;
const snapshot = {
  schemaVersion: "1.0.0",
  generatedAt: new Date().toISOString(),
  fixedGitHubSource: {
    repository: "WenxuanZhang120/investment-research",
    branch: dataBranch,
    directory: "portfolio",
    holdingsFile: "portfolio/holdings.csv",
    executionFile: "portfolio/execution_status.json",
  },
  asOfDate: execution.as_of_date,
  source: execution.source,
  positionCount: positions.length,
  quoteCoverage: { available: quoteCount, total: positions.length },
  executedPrincipalBeforeFees: Number(execution.executed_principal_before_fees),
  estimatedRemainingCashBeforeFees: Number(execution.estimated_remaining_cash_before_fees),
  positions,
};

await mkdir(path.dirname(outputFile), { recursive: true });
await writeFile(outputFile, `${JSON.stringify(snapshot, null, 2)}\n`, "utf8");
console.log(`Portfolio snapshot generated: ${positions.length} positions, ${quoteCount}/${positions.length} quotes.`);
