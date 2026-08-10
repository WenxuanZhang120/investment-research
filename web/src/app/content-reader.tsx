"use client";

import { Fragment, type ReactNode, useEffect } from "react";
import type { ResearchContentItem, ResearchContentType } from "@/lib/research-types";

const readerLabels: Record<ResearchContentType, string> = {
  daily: "日报",
  news: "新闻",
  announcement: "公告",
  report: "研究报告",
};

const categoryLabels: Record<string, string> = {
  other_announcement: "综合资讯",
  restructuring: "资产重组",
  share_repurchase: "股份回购",
  earnings: "业绩动态",
  dividend: "分红派息",
};

type DailyEntry = {
  group: string;
  category: string;
  title: string;
  url: string | null;
  media: string | null;
  publishedAt: string | null;
  source: string | null;
  sourceRecord: string | null;
  facts: Array<{ label: string; value: string }>;
};

type DailyReport = {
  title: string;
  intro: string;
  summary: Array<{ label: string; value: string }>;
  detailLabel: string;
  entries: DailyEntry[];
  emptyMessage: string | null;
};

function cleanInlineCode(value: string) {
  return value.replace(/^`|`$/g, "");
}

function formatPublishedAt(value: string | null) {
  if (!value) return null;
  const match = value.match(/^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match;
  return `${year}年${Number(month)}月${Number(day)}日 ${hour}:${minute}`;
}

function parseLinkedTitle(value: string) {
  const match = value.match(/^\[([^\]]+)]\((https?:\/\/[^)]+)\)$/);
  return match ? { title: match[1], url: match[2] } : { title: value, url: null };
}

function parseDailyReport(content: string): DailyReport | null {
  if (!/^# .*(新闻|公告).*日报/m.test(content) || !/^## (新闻|公告)明细/m.test(content)) return null;

  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const title = lines.find((line) => line.startsWith("# "))?.slice(2).trim() ?? "每日内容索引";
  const summaryStart = lines.findIndex((line) => line.trim() === "## 摘要");
  const detailStart = lines.findIndex((line) => /^## (新闻|公告)明细$/.test(line.trim()));
  if (summaryStart < 0 || detailStart < 0) return null;

  const intro = lines.slice(1, summaryStart).map((line) => line.trim()).filter(Boolean).join(" ");
  const parseFact = (line: string) => {
    const match = line.match(/^- ([^：]+)：\s*(.+)$/);
    return match ? { label: match[1].trim(), value: cleanInlineCode(match[2].trim()) } : null;
  };
  const summary = lines.slice(summaryStart + 1, detailStart).map(parseFact).filter((fact): fact is { label: string; value: string } => fact !== null);
  const detailLabel = lines[detailStart].replace(/^## /, "").trim();
  const detailLines = lines.slice(detailStart + 1);
  const entries: DailyEntry[] = [];
  let currentGroup: string | null = null;
  let currentFacts: Array<{ label: string; value: string }> = [];

  const flushEntry = () => {
    if (!currentGroup || currentFacts.length === 0) return;
    const factMap = new Map(currentFacts.map((fact) => [fact.label, fact.value]));
    const linkedTitle = parseLinkedTitle(factMap.get("标题") ?? currentGroup);
    const [group = currentGroup, category = ""] = currentGroup.split("｜").map((part) => part.trim());
    entries.push({
      group,
      category,
      title: linkedTitle.title,
      url: linkedTitle.url,
      media: factMap.get("媒体") ?? null,
      publishedAt: factMap.get("发布时间") ?? null,
      source: factMap.get("数据来源") ?? factMap.get("来源") ?? null,
      sourceRecord: factMap.get("来源记录") ? cleanInlineCode(factMap.get("来源记录")!) : null,
      facts: currentFacts.filter((fact) => !["标题", "媒体", "发布时间", "数据来源", "来源", "来源记录"].includes(fact.label)),
    });
  };

  for (const rawLine of detailLines) {
    const line = rawLine.trim();
    if (line.startsWith("### ")) {
      flushEntry();
      currentGroup = line.slice(4).trim();
      currentFacts = [];
      continue;
    }
    const fact = parseFact(line);
    if (fact) currentFacts.push(fact);
  }
  flushEntry();

  const emptyMessage = entries.length === 0
    ? detailLines.map((line) => line.trim()).filter(Boolean).join(" ") || "当日没有可展示记录。"
    : null;
  return { title, intro, summary, detailLabel, entries, emptyMessage };
}

function renderInline(text: string): ReactNode[] {
  const pattern = /(\[[^\]]+\]\(https?:\/\/[^)]+\)|`[^`]+`|\*\*[^*]+\*\*)/g;
  return text.split(pattern).filter(Boolean).map((part, index) => {
    const link = part.match(/^\[([^\]]+)]\((https?:\/\/[^)]+)\)$/);
    if (link) return <a key={index} href={link[2]} target="_blank" rel="noreferrer">{link[1]}</a>;
    if (part.startsWith("`") && part.endsWith("`")) return <code key={index}>{part.slice(1, -1)}</code>;
    if (part.startsWith("**") && part.endsWith("**")) return <strong key={index}>{part.slice(2, -2)}</strong>;
    return <Fragment key={index}>{part}</Fragment>;
  });
}

function splitTableRow(line: string) {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

function isTableDivider(line: string) {
  return /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line.trim());
}

function MarkdownDocument({ content, title }: { content: string; title: string }) {
  const lines = content.replace(/\r\n/g, "\n").split("\n");
  const blocks: ReactNode[] = [];
  let index = 0;

  while (index < lines.length) {
    const line = lines[index].trimEnd();
    if (!line.trim()) { index += 1; continue; }
    if (line.startsWith("# ") && line.slice(2).trim() === title.trim()) { index += 1; continue; }

    if (line.startsWith("```")) {
      const language = line.slice(3).trim();
      const code: string[] = [];
      index += 1;
      while (index < lines.length && !lines[index].trim().startsWith("```")) { code.push(lines[index]); index += 1; }
      index += 1;
      blocks.push(<pre className="document-code" key={`code-${index}`}><code data-language={language || undefined}>{code.join("\n")}</code></pre>);
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      const level = heading[1].length;
      const Heading = level <= 2 ? "h2" : "h3";
      blocks.push(<Heading key={`heading-${index}`}>{renderInline(heading[2])}</Heading>);
      index += 1;
      continue;
    }

    if (line.trim().startsWith("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const header = splitTableRow(line);
      const rows: string[][] = [];
      index += 2;
      while (index < lines.length && lines[index].trim().startsWith("|")) { rows.push(splitTableRow(lines[index])); index += 1; }
      blocks.push(<div className="document-table-wrap" key={`table-${index}`}><table><thead><tr>{header.map((cell, cellIndex) => <th key={cellIndex}>{renderInline(cell)}</th>)}</tr></thead><tbody>{rows.map((row, rowIndex) => <tr key={rowIndex}>{row.map((cell, cellIndex) => <td key={cellIndex}>{renderInline(cell)}</td>)}</tr>)}</tbody></table></div>);
      continue;
    }

    if (/^[-*]\s+/.test(line.trim())) {
      const items: string[] = [];
      while (index < lines.length && /^[-*]\s+/.test(lines[index].trim())) { items.push(lines[index].trim().replace(/^[-*]\s+/, "")); index += 1; }
      blocks.push(<ul key={`list-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{renderInline(item)}</li>)}</ul>);
      continue;
    }

    if (/^\d+\.\s+/.test(line.trim())) {
      const items: string[] = [];
      while (index < lines.length && /^\d+\.\s+/.test(lines[index].trim())) { items.push(lines[index].trim().replace(/^\d+\.\s+/, "")); index += 1; }
      blocks.push(<ol key={`ordered-${index}`}>{items.map((item, itemIndex) => <li key={itemIndex}>{renderInline(item)}</li>)}</ol>);
      continue;
    }

    if (line.trim().startsWith(">")) {
      const quote: string[] = [];
      while (index < lines.length && lines[index].trim().startsWith(">")) { quote.push(lines[index].trim().replace(/^>\s?/, "")); index += 1; }
      blocks.push(<blockquote key={`quote-${index}`}>{renderInline(quote.join(" "))}</blockquote>);
      continue;
    }

    const paragraph = [line.trim()];
    index += 1;
    while (index < lines.length) {
      const next = lines[index].trim();
      if (!next || /^(#{1,4})\s+/.test(next) || /^[-*]\s+/.test(next) || /^\d+\.\s+/.test(next) || next.startsWith("|") || next.startsWith("```") || next.startsWith(">")) break;
      paragraph.push(next);
      index += 1;
    }
    blocks.push(<p key={`paragraph-${index}`}>{renderInline(paragraph.join(" "))}</p>);
  }

  return <div className="markdown-document">{blocks}</div>;
}

function DailyReportDocument({ report }: { report: DailyReport }) {
  const publicSummary = report.summary.filter((fact) => !fact.label.includes("_") && !Object.hasOwn(categoryLabels, fact.label));
  const categorySummary = report.summary.filter((fact) => fact.label.includes("_") || Object.hasOwn(categoryLabels, fact.label));
  return <div className="daily-reader-layout">
    <section className="daily-reader-main">
      {report.intro && <div className="reader-boundary-note"><strong>阅读说明</strong><p>{report.intro}</p></div>}
      <div className="daily-section-heading"><div><span>DETAILS</span><h3>{report.detailLabel}</h3></div><strong>{report.entries.length} 条</strong></div>
      {report.entries.length ? <div className="daily-entry-list">{report.entries.map((entry, index) => {
        const category = categoryLabels[entry.category] ?? (entry.category || "记录");
        const publishedAt = formatPublishedAt(entry.publishedAt);
        return <article className="daily-entry-card" key={`${entry.sourceRecord ?? entry.title}-${index}`}>
          <div className="entry-index">{String(index + 1).padStart(2, "0")}</div>
          <div className="entry-body">
            <div className="entry-tags"><span>{category}</span>{entry.group !== "未关联证券" && <em>{entry.group}</em>}</div>
            <h4>{entry.url ? <a href={entry.url} target="_blank" rel="noreferrer">{entry.title}</a> : entry.title}</h4>
            <div className="entry-meta"><strong>{entry.media ?? entry.source ?? "来源未标注"}</strong>{publishedAt && <span>{publishedAt}</span>}</div>
            {entry.facts.length > 0 && <dl className="entry-facts">{entry.facts.map((fact) => <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>)}</dl>}
            <div className="entry-actions">{entry.url && <a href={entry.url} target="_blank" rel="noreferrer">阅读原文 <span>↗</span></a>}<details><summary>数据溯源</summary><div><span>数据来源：{entry.source ?? "未标注"}</span><span>内部分类：{entry.category || "未标注"}</span><span>来源记录：{entry.sourceRecord ?? "未标注"}</span></div></details></div>
          </div>
        </article>;
      })}</div> : <div className="daily-empty"><span>—</span><h4>当日没有记录</h4><p>{report.emptyMessage}</p></div>}
    </section>
    <aside className="daily-reader-aside">
      <span className="aside-eyebrow">TODAY AT A GLANCE</span>
      <h3>今日摘要</h3>
      <div className="daily-summary-stats">{publicSummary.map((fact) => <div key={fact.label}><strong>{fact.value}</strong><span>{fact.label}</span></div>)}</div>
      {categorySummary.length > 0 && <div className="daily-category-summary"><span>内容分类</span>{categorySummary.map((fact) => <div key={fact.label}><em>{categoryLabels[fact.label] ?? fact.label}</em><strong>{fact.value}</strong></div>)}</div>}
      <div className="aside-note"><strong>事实边界</strong><p>日报用于建立事实索引。重要信息仍应回到原始来源和公司公告交叉验证。</p></div>
    </aside>
  </div>;
}

function GenericReaderDocument({ item }: { item: ResearchContentItem }) {
  return <div className="generic-reader-layout"><section className="generic-reader-main">{item.content ? <MarkdownDocument content={item.content} title={item.title} /> : <div className="article-summary"><span>内容摘要</span><p>{item.summary}</p></div>}{item.securityName && <div className="reader-security-meta"><span>关联证券</span><strong>{item.securityName} · {item.securityCode}</strong></div>}</section><aside className="generic-reader-aside"><span>DOCUMENT INFO</span><h3>文档信息</h3><dl><div><dt>内容类型</dt><dd>{readerLabels[item.type]}</dd></div><div><dt>归档日期</dt><dd>{item.date.slice(0, 10)}</dd></div><div><dt>来源</dt><dd>{item.source}</dd></div></dl><div className="aside-note"><strong>阅读提示</strong><p>数据验证与研究文档保留原始表格、列表和技术字段，便于复核结论。</p></div></aside></div>;
}

export default function ContentReader({ item, onClose }: { item: ResearchContentItem; onClose: () => void }) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const dailyReport = item.content ? parseDailyReport(item.content) : null;
  return <div className="drawer-backdrop reader-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}><article className="reader-drawer" role="dialog" aria-modal="true" aria-label={item.title}>
    <header><div><div className="reader-kicker"><span className={`content-type type-${item.type}`}>{readerLabels[item.type]}</span><span>{item.date.slice(0, 10)}</span><span>{item.source}</span></div><h2>{item.title}</h2></div><button className="icon-button" onClick={onClose} aria-label="关闭阅读器">×</button></header>
    <div className="reader-content">{dailyReport ? <DailyReportDocument report={dailyReport} /> : <GenericReaderDocument item={item} />}</div>
    <footer>{item.url ? <a className="primary-action link-action" href={item.url} target="_blank" rel="noreferrer">打开原始来源</a> : <span className="source-note">内容来自仓库内已归档文档</span>}<button className="secondary-action" onClick={onClose}>关闭</button></footer>
  </article></div>;
}
