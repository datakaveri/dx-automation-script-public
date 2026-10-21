import { z } from "zod";

const optionalTrimmedString = z
  .string()
  .optional()
  .transform((val) => (val && val.trim().length > 0 ? val.trim() : undefined));

const envSchema = z
  .object({
    COLLECTIONS_DIR: z.string().min(1).default("collections"),
    CONTROL_PLANE_PASSWORD_FILE: optionalTrimmedString,
    DATABASE_URL: z.string().url("DATABASE_URL must be a valid PostgreSQL URL.").refine(
      (value) => value.startsWith("postgres://") || value.startsWith("postgresql://"),
      "DATABASE_URL must use the postgres:// or postgresql:// protocol."
    ),
    DB_SYNC: z.enum(["alter", "none"]).default("alter"),
    NODE_ENV: z.enum(["development", "test", "production"]).default("development"),
    PORT: z.coerce.number().int().min(1).max(65535).default(4000),
    CORS_ORIGINS: z.string().default("http://localhost:5173,http://127.0.0.1:5173"),
    SMTP_HOST: optionalTrimmedString,
    SMTP_PORT: z.coerce.number().int().min(1).max(65535).default(587),
    SMTP_USER: optionalTrimmedString,
    SMTP_PASS: optionalTrimmedString, // gitleaks:allow
    MAIL_FROM: optionalTrimmedString,
    QA_RECIPIENTS: z.string().default(""),
    PUBLIC_API_URL: z.string().url().default("http://localhost:4000"),
    DASHBOARD_URL: z.string().url().default("https://iudxqa-dev.slicearrow.com"),
    AUTO_RERUN_ON_STARTUP: z.stringbool().default(false),
    KEYCLOAK_URL: optionalTrimmedString,
    KEYCLOAK_REALM: optionalTrimmedString,
    KEYCLOAK_CLIENT_ID: optionalTrimmedString,
    KEYCLOAK_FRONTEND_CLIENT_ID: optionalTrimmedString,
    KEYCLOAK_API_AUDIENCE: optionalTrimmedString,
    RESULTS_FILE: optionalTrimmedString,
    POST_RUN_VENV_ROOT: optionalTrimmedString
  })
  .superRefine((config, context) => {
    if (config.NODE_ENV === "production" && config.DB_SYNC === "alter") {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ["DB_SYNC"],
        message: "DB_SYNC=alter is not allowed in production."
      });
    }
  });

export type AppConfig = z.infer<typeof envSchema>;

export function loadConfig(environment: NodeJS.ProcessEnv): AppConfig {
  const result = envSchema.safeParse(environment);
  if (!result.success) {
    const details = result.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`).join("; ");
    throw new Error(`Invalid environment configuration: ${details}`);
  }
  return result.data;
}
