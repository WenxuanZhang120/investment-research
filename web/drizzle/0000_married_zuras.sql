CREATE TABLE "decision_logs" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"owner_login" varchar(100) NOT NULL,
	"decision_date" date NOT NULL,
	"security_code" varchar(32) NOT NULL,
	"security_name" varchar(120) NOT NULL,
	"decision_type" varchar(16) NOT NULL,
	"reason" text NOT NULL,
	"evidence" text NOT NULL,
	"risks" text NOT NULL,
	"confidence" varchar(16) NOT NULL,
	"review_date" date,
	"status" varchar(16) NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"updated_at" timestamp with time zone DEFAULT now() NOT NULL
);
