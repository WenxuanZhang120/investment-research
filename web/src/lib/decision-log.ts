import { and, desc, eq } from "drizzle-orm";
import { getDatabase, isDatabaseConfigured } from "@/db";
import { decisionLogs, type DecisionLog } from "@/db/schema";
import { requireAllowedUser } from "@/lib/authorization";

export type DecisionLogResult = {
  configured: boolean;
  available: boolean;
  entries: DecisionLog[];
  message: string | null;
};

export async function getDecisionLogs(): Promise<DecisionLogResult> {
  const { login } = await requireAllowedUser();
  if (!isDatabaseConfigured()) {
    return { configured: false, available: false, entries: [], message: "私有日志数据库尚未连接" };
  }
  try {
    const entries = await getDatabase()
      .select()
      .from(decisionLogs)
      .where(eq(decisionLogs.ownerLogin, login))
      .orderBy(desc(decisionLogs.decisionDate), desc(decisionLogs.updatedAt));
    return { configured: true, available: true, entries, message: null };
  } catch {
    return { configured: true, available: false, entries: [], message: "数据库暂时不可用，请稍后重试" };
  }
}

export async function updateOwnedDecision(id: string, login: string, values: Partial<typeof decisionLogs.$inferInsert>) {
  await getDatabase()
    .update(decisionLogs)
    .set({ ...values, updatedAt: new Date() })
    .where(and(eq(decisionLogs.id, id), eq(decisionLogs.ownerLogin, login)));
}
