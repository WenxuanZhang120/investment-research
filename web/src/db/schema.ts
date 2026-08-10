import { date, pgTable, text, timestamp, uuid, varchar } from "drizzle-orm/pg-core";

export const decisionLogs = pgTable("decision_logs", {
  id: uuid("id").defaultRandom().primaryKey(),
  ownerLogin: varchar("owner_login", { length: 100 }).notNull(),
  decisionDate: date("decision_date").notNull(),
  securityCode: varchar("security_code", { length: 32 }).notNull(),
  securityName: varchar("security_name", { length: 120 }).notNull(),
  decisionType: varchar("decision_type", { length: 16 }).notNull(),
  reason: text("reason").notNull(),
  evidence: text("evidence").notNull(),
  risks: text("risks").notNull(),
  confidence: varchar("confidence", { length: 16 }).notNull(),
  reviewDate: date("review_date"),
  status: varchar("status", { length: 16 }).notNull(),
  createdAt: timestamp("created_at", { withTimezone: true }).defaultNow().notNull(),
  updatedAt: timestamp("updated_at", { withTimezone: true }).defaultNow().notNull(),
});

export type DecisionLog = typeof decisionLogs.$inferSelect;
