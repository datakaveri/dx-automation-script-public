import { randomUUID } from "node:crypto";
import express from "express";
import swaggerUi from "swagger-ui-express";
import type winston from "winston";
import { CollectionController } from "./controllers/collectionController";
import { SettingsController } from "./controllers/settingsController";
import { openApiDocument } from "./docs/openapi";
import { createApiRouter } from "./routes/apiRoutes";
import { requireAuthentication } from "./authentication";
import { validateRouteParam } from "./validation";

export function createApp(controller: CollectionController, logger: winston.Logger, corsOrigins: string[], settingsController: SettingsController): express.Express {
  const app = express();
  app.disable("x-powered-by");
  app.use(express.json({ limit: "16kb" }));
  const allowedOrigins = new Set(corsOrigins);
  // CORS Middleware
  app.use((request, response, next) => {
    const origin = request.header("origin");
    if (origin) {
      if (allowedOrigins.has(origin) || allowedOrigins.has("*")) {
        response.setHeader("Access-Control-Allow-Origin", origin);
      }
    } else {
      response.setHeader("Access-Control-Allow-Origin", "*");
    }
    response.setHeader("Vary", "Origin");
    response.setHeader("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,PATCH,OPTIONS");
    response.setHeader("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Request-ID,X-Requested-With,Accept,Origin");
    response.setHeader("Access-Control-Allow-Credentials", "true");

    if (request.method === "OPTIONS") {
      response.status(204).end();
      return;
    }
    next();
  });

  // Health check endpoints for Pangolin and load balancers
  app.get(["/", "/health", "/healthz"], (_request, response) => {
    response.status(200).json({
      status: "healthy",
      service: "iudxqa-backend",
      version: "1.0.0",
      timestamp: new Date().toISOString()
    });
  });

  app.use((request, response, next) => {
    const requestId = request.header("x-request-id") || randomUUID();
    response.locals.requestId = requestId;
    response.setHeader("x-request-id", requestId);
    const startedAt = Date.now();
    response.on("finish", () => {
      logger.info("HTTP request", {
        requestId,
        method: request.method,
        path: request.originalUrl,
        statusCode: response.statusCode,
        durationMs: Date.now() - startedAt
      });
    });
    next();
  });
  app.use((_request, response, next) => {
    response.setHeader("X-Content-Type-Options", "nosniff");
    response.setHeader("X-Frame-Options", "SAMEORIGIN");
    next();
  });

  // Keep the API reference public; callers can authorize from Swagger UI and
  // the API routes below still validate bearer tokens server-side.
  app.get("/api/docs.json", (_request, response) => response.json(openApiDocument));
  app.use("/api/docs", swaggerUi.serve, swaggerUi.setup(openApiDocument, {
    customSiteTitle: "IUDX QA Backend API Docs"
  }));

  // HTML reports are public so browser and email links can open them directly.
  app.get("/api/collections/:id/report", validateRouteParam("id"), controller.report.bind(controller));
  app.get(
    "/api/collections/:id/history/:runId/report",
    validateRouteParam("id"),
    validateRouteParam("runId"),
    controller.historyReport.bind(controller),
  );

  // Require authentication for API operations, excluding the public docs above.
  app.use("/api", requireAuthentication);

  app.get("/api/auth/me", (request, response) => {
    const claims = request.user;
    if (!claims) {
      response.status(401).json({ error: { code: "UNAUTHORIZED", message: "A valid bearer token is required." } });
      return;
    }
    response.json({
      user: {
        id: claims.sub,
        username: claims.preferred_username ?? null,
        email: claims.email ?? null,
        name: claims.name ?? null,
        roles: {
          realm: claims.realm_access?.roles ?? [],
          clients: claims.resource_access ?? {},
          groups: claims.groups ?? [],
        },
      },
    });
  });

  app.use("/api", createApiRouter(controller, settingsController));

  app.use((error: unknown, _request: express.Request, response: express.Response, _next: express.NextFunction) => {
    const status = typeof error === "object" && error !== null && "status" in error
      ? (error as { status?: unknown }).status
      : undefined;
    if (status === 400) {
      response.status(400).json({ error: { code: "VALIDATION_ERROR", message: "Request body must contain valid JSON." } });
      return;
    }
    logger.error("Unhandled request error", {
      requestId: response.locals.requestId,
      error: error instanceof Error ? { message: error.message, stack: error.stack } : error
    });
    response.status(500).json({ error: { code: "INTERNAL_ERROR", message: "The server could not complete this request." } });
  });

  return app;
}
