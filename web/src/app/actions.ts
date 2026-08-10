"use server";

import { revalidatePath } from "next/cache";
import { z } from "zod";
import { signIn, signOut } from "@/auth";
import { getDatabase } from "@/db";
import { decisionLogs } from "@/db/schema";
import { requireAllowedUser } from "@/lib/authorization";
import { updateOwnedDecision } from "@/lib/decision-log";

const decisionSchema = z.object({
  decisionDate: z.iso.date(),
  securityCode: z.string().trim().min(1).max(32),
  securityName: z.string().trim().min(1).max(120),
  decisionType: z.enum(["BUY", "WATCH", "HOLD", "ADD", "TRIM", "EXIT", "REVIEW"]),
  reason: z.string().trim().min(1).max(4000),
  evidence: z.string().trim().min(1).max(4000),
  risks: z.string().trim().min(1).max(4000),
  confidence: z.enum(["LOW", "MEDIUM", "HIGH"]),
  reviewDate: z.union([z.iso.date(), z.literal("")]),
  status: z.enum(["DRAFT", "ACTIVE", "REVIEWED", "CLOSED"]),
});

function parseDecision(formData: FormData) {
  return decisionSchema.parse(Object.fromEntries(formData.entries()));
}

export async function loginWithGitHub() {
  await signIn("github", { redirectTo: "/" });
}

export async function logout() {
  await signOut({ redirectTo: "/" });
}

export async function createDecision(formData: FormData) {
  const { login } = await requireAllowedUser();
  const values = parseDecision(formData);
  await getDatabase().insert(decisionLogs).values({
    ownerLogin: login,
    ...values,
    reviewDate: values.reviewDate || null,
  });
  revalidatePath("/");
}

export async function updateDecision(formData: FormData) {
  const { login } = await requireAllowedUser();
  const id = z.uuid().parse(formData.get("id"));
  const values = parseDecision(formData);
  await updateOwnedDecision(id, login, { ...values, reviewDate: values.reviewDate || null });
  revalidatePath("/");
}
