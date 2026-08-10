export type PortfolioPosition = {
  asOfDate: string;
  securityCode: string;
  securityName: string;
  quantity: number;
  averageCost: number;
  currency: string;
  costBasis: number;
  targetWeight: number | null;
  notes: string;
  latestPrice: number | null;
  quoteDate: string | null;
  marketValue: number | null;
  quoteSource: string | null;
  quoteSourceFile: string | null;
  quoteStatus: "available" | "missing";
};

export type PortfolioSnapshot = {
  schemaVersion: string;
  generatedAt: string;
  fixedGitHubSource: {
    repository: string;
    branch: string;
    directory: string;
    holdingsFile: string;
    executionFile: string;
  };
  asOfDate: string;
  source: string;
  positionCount: number;
  quoteCoverage: { available: number; total: number };
  executedPrincipalBeforeFees: number;
  estimatedRemainingCashBeforeFees: number;
  positions: PortfolioPosition[];
  liveData?: {
    status: "live" | "fallback";
    branch: string;
    checkedAt: string;
    message: string | null;
  };
};
