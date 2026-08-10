import financialsJson from "@/generated/security-financials.json";
import { auth } from "@/auth";
import type { FinancialPeriod } from "@/lib/research-types";

export const dynamic = "force-dynamic";

const financials = financialsJson as unknown as Record<string, FinancialPeriod[]>;

export async function GET(_request: Request, context: { params: Promise<{ code: string }> }) {
  const session = await auth();
  const allowedLogin = (process.env.ALLOWED_GITHUB_LOGIN ?? "WenxuanZhang120").toLowerCase();
  if (session?.user?.githubLogin?.toLowerCase() !== allowedLogin) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { code } = await context.params;
  const securityCode = decodeURIComponent(code).toUpperCase();
  return Response.json({ securityCode, periods: financials[securityCode] ?? [] });
}
