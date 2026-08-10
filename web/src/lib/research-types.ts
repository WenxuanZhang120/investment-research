export type ResearchPriority = "P0" | "P1" | "P2" | "Reject";

export type ScreeningSecurity = {
  securityCode: string;
  securityName: string;
  rank: number;
  priority: ResearchPriority;
  score: number;
  eligible: boolean;
  eligibilityReasons: string[];
  marketCap: number | null;
  peTtm: number | null;
  netProfitMargin: number | null;
  operatingCashFlowMargin: number | null;
  financialPeriodEnd: string | null;
  scoreComponents: {
    marketCap: number | null;
    peTtm: number | null;
    netProfitMargin: number | null;
    operatingCashFlowMargin: number | null;
  };
};

export type QuoteSnapshot = {
  close: number;
  tradeDate: string;
  currency: string;
  source: string;
};

export type FinancialPeriod = {
  periodEnd: string;
  reportType: string;
  reportLabel: string;
  filingDate: string | null;
  facts: Record<string, number | null>;
  metrics: Record<string, number | null>;
};

export type ResearchContentType = "daily" | "news" | "announcement" | "report";

export type ResearchContentItem = {
  id: string;
  type: ResearchContentType;
  title: string;
  date: string;
  source: string;
  url: string | null;
  summary: string;
  content: string | null;
  securityCode: string | null;
  securityName: string | null;
  tags: string[];
};

export type ResearchSnapshot = {
  schemaVersion: string;
  generatedAt: string;
  asOfDate: string;
  screeningRunId: string;
  screeningSourceFile: string;
  securities: ScreeningSecurity[];
  quotes: Record<string, QuoteSnapshot>;
  content: ResearchContentItem[];
  coverage: {
    screeningTotal: number;
    priorityCounts: Record<ResearchPriority, number>;
    quoteCount: number;
    financialSecurityCount: number;
    newsCount: number;
    announcementCount: number;
    dailyCount: number;
    reportCount: number;
    institutionalResearchCount: number;
  };
  sources: Array<{
    label: string;
    path: string;
    asOfDate: string;
    records: number;
    status: "ready" | "partial" | "missing";
  }>;
  liveData?: {
    status: "live" | "fallback";
    branch: string;
    latestDate: string | null;
    checkedAt: string;
    message: string | null;
  };
};

export type SecurityFinancialResponse = {
  securityCode: string;
  periods: FinancialPeriod[];
};
