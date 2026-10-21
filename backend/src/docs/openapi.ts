const json = (schema: Record<string, unknown>) => ({
  content: { "application/json": { schema } },
});

const errorResponse = {
  description: "Request failed.",
  ...json({ $ref: "#/components/schemas/ErrorResponse" }),
};

const idParameter = (name: string, description: string) => ({
  name,
  in: "path",
  required: true,
  description,
  schema: { type: "string", minLength: 1 },
});

const runStatusSchema = { $ref: "#/components/schemas/CollectionStatus" };

export const openApiDocument = {
  openapi: "3.0.3",
  info: {
    title: "IUDX QA Backend API",
    version: "1.0.0",
    description:
      "API for discovering Postman collections, running Newman tests, viewing results, history, and reports. Collection folders are scanned from the configured collections directory. Runs execute asynchronously through Newman and persist their normalized results in PostgreSQL.",
  },
  servers: [{ url: "/", description: "Current backend server" }],
  security: [{ bearerAuth: [] }],
  tags: [
    { name: "Health", description: "Service health checks" },
    {
      name: "Dashboard",
      description: "Collection discovery and dashboard metrics",
    },
    {
      name: "Execution",
      description: "Start collections and inspect run status",
    },
    { name: "Results", description: "Latest results, history, and reports" },
    {
      name: "Settings",
      description: "Auto re-run and email notification settings",
    },
  ],
  paths: {
    "/": {
      get: {
        tags: ["Health"],
        summary: "Check service health",
        description:
          "Read-only health check for humans and load balancers. A successful response confirms that the HTTP service is accepting requests.",
        operationId: "healthCheck",
        security: [],
        responses: {
          "200": {
            description: "Service is healthy.",
            ...json({ $ref: "#/components/schemas/HealthResponse" }),
          },
        },
      },
    },
    "/health": {
      get: {
        tags: ["Health"],
        summary: "Check service health",
        description:
          "Alias of the root health check. Use this endpoint when the deployment platform expects a conventional `/health` path.",
        operationId: "healthCheckAlias",
        security: [],
        responses: {
          "200": {
            description: "Service is healthy.",
            ...json({ $ref: "#/components/schemas/HealthResponse" }),
          },
        },
      },
    },
    "/healthz": {
      get: {
        tags: ["Health"],
        summary: "Check service health for orchestration",
        description:
          "Read-only health check intended for Kubernetes-style liveness or readiness probes. It does not execute a collection or query collection results.",
        operationId: "healthCheckOrchestration",
        security: [],
        responses: {
          "200": {
            description: "Service is healthy.",
            ...json({ $ref: "#/components/schemas/HealthResponse" }),
          },
        },
      },
    },
    "/api/dashboard": {
      get: {
        tags: ["Dashboard"],
        summary: "Get dashboard KPIs and collection rows",
        description:
          "Returns the dashboard view used by the frontend. The backend scans valid collection folders, reloads persisted collection state from PostgreSQL, calculates KPI totals, and returns one row per recognized collection. This endpoint is read-only and does not run Newman.",
        operationId: "getDashboard",
        responses: {
          "200": {
            description: "Dashboard data.",
            ...json({ $ref: "#/components/schemas/DashboardResponse" }),
          },
          "500": errorResponse,
        },
      },
    },
    "/api/summary": {
      get: {
        tags: ["Dashboard"],
        summary: "Get aggregate collection metrics",
        description:
          "Returns aggregate counters for all recognized collections, including collection status counts, total API requests, failed assertions, and weighted average response time. This is a compact metrics response compared with `/api/dashboard`.",
        operationId: "getSummary",
        responses: {
          "200": {
            description: "Aggregate metrics.",
            ...json({ $ref: "#/components/schemas/SummaryResponse" }),
          },
          "500": errorResponse,
        },
      },
    },
    "/api/collections": {
      get: {
        tags: ["Dashboard"],
        summary: "List discovered collections",
        description:
          "Scans the configured collections directory and returns collection metadata combined with the latest persisted execution state. A folder is recognized only when it contains exactly one valid Postman collection JSON and one valid environment JSON.",
        operationId: "listCollections",
        responses: {
          "200": {
            description: "Discovered collections.",
            ...json({
              type: "array",
              items: { $ref: "#/components/schemas/CollectionSummary" },
            }),
          },
          "500": errorResponse,
        },
      },
    },
    "/api/skipped": {
      get: {
        tags: ["Dashboard"],
        summary: "List skipped collection files and folders",
        description:
          "Returns collection files or folders that could not be included in the catalog. Use this endpoint to diagnose invalid JSON, unsupported files, or folders missing exactly one collection and one environment file.",
        operationId: "listSkippedFiles",
        responses: {
          "200": {
            description: "Skipped files and reasons.",
            ...json({
              type: "array",
              items: { $ref: "#/components/schemas/SkippedFile" },
            }),
          },
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/run": {
      post: {
        tags: ["Execution"],
        summary: "Start one collection run",
        description:
          "Starts an asynchronous Newman execution for one recognized collection. The server marks the collection as `running`, creates a `runId`, queues the work, and immediately returns `202`. Poll the returned `statusUrl` until the status is `passed`, `failed`, or `error`. No request body is required. Returns `409` when the collection is already running.",
        operationId: "runCollection",
        parameters: [
          idParameter(
            "id",
            "Collection identifier derived from its folder name.",
          ),
        ],
        responses: {
          "202": {
            description: "Collection accepted for asynchronous execution.",
            ...json({ $ref: "#/components/schemas/RunStartResponse" }),
          },
          "404": errorResponse,
          "409": errorResponse,
          "422": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/run-all": {
      post: {
        tags: ["Execution"],
        summary: "Queue all discovered collections",
        description:
          "Queues every recognized collection that is not already running. Invalid collections and collections already in progress are returned in `skipped`. Execution is asynchronous and the runner allows up to 10 active collection runs at once.",
        operationId: "runAllCollections",
        responses: {
          "202": {
            description: "Collections queued for execution.",
            ...json({ $ref: "#/components/schemas/RunAllResponse" }),
          },
          "500": errorResponse,
        },
      },
    },
    "/api/run-selected": {
      post: {
        tags: ["Execution"],
        summary: "Queue selected collection runs",
        description:
          "Accepts a list of collection IDs and queues every recognized, valid collection that is not already running. Each accepted collection gets its own run ID and status URL. The response also lists requested IDs that were not found, invalid, or already running. The operation is asynchronous; poll each accepted status URL. Up to 50 unique IDs can be submitted, and the runner allows up to 10 active collection runs at once.",
        operationId: "runSelectedCollections",
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/SelectedRunRequest" },
              example: { collectionIds: ["data-plane", "file-connect"] },
            },
          },
        },
        responses: {
          "202": {
            description:
              "Valid selected collections were queued; skipped selections are reported in the response.",
            ...json({ $ref: "#/components/schemas/SelectedRunResponse" }),
          },
          "400": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/runs/{runId}": {
      get: {
        tags: ["Execution"],
        summary: "Get asynchronous run status",
        description:
          "Returns the current state of a run started by a collection execution endpoint. Use this endpoint for polling instead of repeatedly refreshing the full dashboard. While running, completion fields and stats may be null; after completion they contain the final run metrics.",
        operationId: "getRunStatus",
        parameters: [
          idParameter(
            "runId",
            "Run identifier returned when execution starts.",
          ),
        ],
        responses: {
          "200": {
            description: "Current run status.",
            ...json({ $ref: "#/components/schemas/RunStatusResponse" }),
          },
          "404": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/result": {
      get: {
        tags: ["Results"],
        summary: "Get the latest stored result",
        description:
          "Returns the complete normalized result from the most recent completed collection run, including request executions, response codes, latency, response sizes, assertion results, and aggregate statistics. The result is `null` when the collection has not completed a run. This endpoint does not start a new run.",
        operationId: "getLatestResult",
        parameters: [idParameter("id", "Collection identifier.")],
        responses: {
          "200": {
            description:
              "Latest result, or null when the collection has not completed a run.",
            ...json({ $ref: "#/components/schemas/ResultResponse" }),
          },
          "404": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/runs/latest": {
      get: {
        tags: ["Results"],
        summary: "Get and filter endpoints from the latest run",
        description:
          "Returns endpoint-level data from the latest completed run. Filtering is performed against endpoint name, pass/fail status, HTTP method, and Postman folder. Results are paginated with `page` and `pageSize`; the response also includes collection KPIs and the available folder counts. This endpoint does not execute Newman.",
        operationId: "getLatestRun",
        parameters: [
          idParameter("id", "Collection identifier."),
          {
            name: "search",
            in: "query",
            schema: { type: "string", default: "" },
            description: "Search endpoint names.",
          },
          {
            name: "status",
            in: "query",
            schema: {
              type: "string",
              enum: ["all", "passed", "failed"],
              default: "all",
            },
            description:
              "Return all endpoints or only passed/failed endpoints.",
          },
          {
            name: "method",
            in: "query",
            schema: {
              type: "string",
              enum: ["all", "GET", "POST", "PUT", "PATCH", "DELETE"],
              default: "all",
            },
            description: "Return all endpoints or only one HTTP method.",
          },
          {
            name: "folder",
            in: "query",
            schema: { type: "string", default: "all" },
            description: "Filter by Postman folder.",
          },
          {
            name: "page",
            in: "query",
            schema: { type: "integer", minimum: 1, default: 1 },
            description: "One-based result page.",
          },
          {
            name: "pageSize",
            in: "query",
            schema: { type: "integer", minimum: 1, maximum: 100, default: 100 },
            description: "Number of endpoint records per page; maximum 100.",
          },
        ],
        responses: {
          "200": {
            description:
              "Filtered endpoint results from the latest completed run.",
            ...json({ $ref: "#/components/schemas/LatestRunResponse" }),
          },
          "400": errorResponse,
          "404": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/history": {
      get: {
        tags: ["Results"],
        summary: "Get the latest five collection runs",
        description:
          "Returns up to five completed runs for a collection, newest first. Each history item contains execution time, duration, request count, assertion totals, average latency, status, and a link to its HTML report. Reading history does not start a new run.",
        operationId: "getCollectionHistory",
        parameters: [idParameter("id", "Collection identifier.")],
        responses: {
          "200": {
            description: "Collection run history.",
            ...json({ $ref: "#/components/schemas/HistoryResponse" }),
          },
          "404": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/report": {
      get: {
        tags: ["Results"],
        summary: "Render the latest collection report",
        description:
          "Generates an HTML report from the latest stored result. It reads persisted run data and renders the report on demand; it does not execute Newman again. Open the response in a browser or use the returned URL from the dashboard/latest-run response.",
        operationId: "getLatestReport",
        security: [],
        parameters: [idParameter("id", "Collection identifier.")],
        responses: {
          "200": {
            description: "Generated HTML report.",
            content: { "text/html": { schema: { type: "string" } } },
          },
          "404": { description: "Collection not found." },
          "500": errorResponse,
        },
      },
    },
    "/api/collections/{id}/history/{runId}/report": {
      get: {
        tags: ["Results"],
        summary: "Render a historical collection report",
        description:
          "Public endpoint: no bearer token is required, so report links in email open directly in a browser. Generates an HTML report for one specific historical run. The report is built from the stored run identified by `runId`; it does not re-run the collection. Returns `404` when either the collection or historical run does not exist.",
        security: [],
        operationId: "getHistoryReport",
        parameters: [
          idParameter("id", "Collection identifier."),
          idParameter("runId", "Historical run identifier."),
        ],
        responses: {
          "200": {
            description: "Generated HTML report.",
            content: { "text/html": { schema: { type: "string" } } },
          },
          "404": errorResponse,
          "500": errorResponse,
        },
      },
    },
    "/api/settings/auto-rerun": {
      get: {
        tags: ["Settings"],
        summary: "Get auto re-run settings",
        description:
          "Returns the configured auto re-run interval, QA recipients, and collectionIds. A null collectionIds value means all eligible collections; an array limits scheduled runs to those collection IDs.",
        operationId: "getAutoRerunSettings",
        responses: {
          "200": {
            description: "Current auto re-run settings.",
            ...json({ $ref: "#/components/schemas/AutoRerunSettingsResponse" }),
          },
          "500": errorResponse,
        },
      },
      put: {
        tags: ["Settings"],
        summary: "Update auto re-run settings",
        description:
          "Requires the qa_admin realm role. Updates any supplied settings and preserves omitted fields. Set collectionIds to one or more IDs from GET /api/collections to run only those collections automatically (for example, select 3 out of 5); set it to null to restore all eligible collections. Empty arrays, duplicate IDs, and unknown or unavailable IDs return 400. Settings persist across restarts. Saving any settings resets the interval countdown without starting a run immediately; off disables future triggers without cancelling an active cycle. Selected collections use the shared runner with up to 10 concurrent executions. Scheduled email reports include only the selected collections. If a selected collection later disappears, the scheduled cycle fails and attempts to send a failure alert. Manual runs do not use this selection and never send email; POST /api/run-selected runs a chosen subset immediately.",
        operationId: "updateAutoRerunSettings",
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/AutoRerunSettings" },
              examples: {
                selectedCollections: {
                  summary: "Schedule three collections",
                  value: {
                    interval: "15m",
                    collectionIds: [
                      "control-plane",
                      "data-plane",
                      "file-connect",
                    ],
                    recipients: ["qa-team@example.com"],
                  },
                },
                oneCollection: {
                  summary: "Schedule one collection",
                  value: { collectionIds: ["control-plane"] },
                },
                allCollections: {
                  summary: "Restore all collections",
                  value: { collectionIds: null },
                },
                disable: {
                  summary: "Disable automatic runs",
                  value: { interval: "off" },
                },
              },
            },
          },
        },
        responses: {
          "200": {
            description: "Updated auto re-run settings.",
            ...json({ $ref: "#/components/schemas/AutoRerunSettingsResponse" }),
          },
          "400": errorResponse,
          "500": errorResponse,
        },
      },
    },
  },
  components: {
    securitySchemes: {
      bearerAuth: {
        type: "http",
        scheme: "bearer",
        bearerFormat: "JWT",
        description:
          "Enter a Keycloak access token. Swagger UI will send it as an Authorization: Bearer header.",
      },
    },
    schemas: {
      CollectionStatus: {
        type: "string",
        enum: ["idle", "running", "passed", "failed", "error"],
      },
      ErrorResponse: {
        type: "object",
        required: ["error"],
        properties: {
          error: {
            type: "object",
            required: ["code", "message"],
            properties: {
              code: { type: "string", example: "COLLECTION_NOT_FOUND" },
              message: { type: "string", example: "Collection not found." },
              details: { type: "object", additionalProperties: true },
            },
          },
        },
      },
      HealthResponse: {
        type: "object",
        required: ["status", "service", "version", "timestamp"],
        properties: {
          status: { type: "string", example: "healthy" },
          service: { type: "string", example: "iudxqa-backend" },
          version: { type: "string", example: "1.0.0" },
          timestamp: { type: "string", format: "date-time" },
        },
      },
      RunStats: {
        type: "object",
        required: ["requests", "assertions", "tests", "averageResponseTimeMs"],
        properties: {
          requests: { $ref: "#/components/schemas/RequestStats" },
          assertions: { $ref: "#/components/schemas/AssertionStats" },
          tests: { $ref: "#/components/schemas/TestStats" },
          averageResponseTimeMs: { type: "integer", minimum: 0 },
        },
      },
      RequestStats: {
        type: "object",
        required: ["total", "failed"],
        properties: {
          total: { type: "integer", minimum: 0 },
          failed: { type: "integer", minimum: 0 },
        },
      },
      AssertionStats: {
        type: "object",
        required: ["total", "failed"],
        properties: {
          total: { type: "integer", minimum: 0 },
          failed: { type: "integer", minimum: 0 },
        },
      },
      TestStats: {
        type: "object",
        required: ["total", "failed"],
        description:
          "Test counts use one test per executed collection request. Failed tests equal failed requests; assertion counts are reported separately.",
        properties: {
          total: { type: "integer", minimum: 0 },
          failed: { type: "integer", minimum: 0 },
        },
      },
      AssertionResult: {
        type: "object",
        required: ["name", "passed", "errorMessage"],
        properties: {
          name: { type: "string" },
          passed: { type: "boolean" },
          errorMessage: { type: "string", nullable: true },
        },
      },
      ExecutionResult: {
        type: "object",
        required: [
          "folder",
          "name",
          "method",
          "url",
          "statusCode",
          "responseTimeMs",
          "responseSize",
          "assertions",
          "requestError",
          "failed",
        ],
        properties: {
          folder: { type: "string", nullable: true },
          name: { type: "string" },
          method: { type: "string" },
          url: { type: "string" },
          statusCode: { type: "integer", nullable: true },
          responseTimeMs: { type: "integer", nullable: true },
          responseSize: { type: "integer", nullable: true },
          assertions: {
            type: "array",
            items: { $ref: "#/components/schemas/AssertionResult" },
          },
          requestError: { type: "string", nullable: true },
          failed: { type: "boolean" },
          requestHeaders: {
            type: "array",
            description:
              "Headers the request really sent. Authorization, Proxy-Authorization, Cookie and X-API-Key values keep only their scheme (e.g. `Bearer [REDACTED]`); secret environment values are redacted. Absent on runs stored before this field existed.",
            items: {
              type: "object",
              required: ["key", "value"],
              properties: { key: { type: "string" }, value: { type: "string" } },
            },
          },
          requestBody: {
            type: "string",
            nullable: true,
            description:
              "Body the request really sent (raw text, or key=value pairs for form bodies), up to 20,000 characters. Fields whose names contain pass, secret, token or api key are masked and secret environment values are redacted. Null when the request had no body; absent on runs stored before this field existed.",
          },
        },
      },
      SkippedRequest: {
        type: "object",
        description: "A collection request that did not run, for example because a script jumped over it with postman.setNextRequest().",
        required: ["folder", "name", "method", "url"],
        properties: {
          folder: { type: "string", nullable: true },
          name: { type: "string" },
          method: { type: "string" },
          url: { type: "string" },
        },
      },
      RunResult: {
        type: "object",
        required: [
          "startedAt",
          "completedAt",
          "durationMs",
          "stats",
          "executions",
        ],
        properties: {
          startedAt: { type: "string", format: "date-time" },
          completedAt: { type: "string", format: "date-time" },
          durationMs: { type: "integer", minimum: 0 },
          stats: { $ref: "#/components/schemas/RunStats" },
          skippedRequests: {
            type: "array",
            description: "Collection requests that did not run. Request and test totals count only requests that ran.",
            items: { $ref: "#/components/schemas/SkippedRequest" },
          },
          executions: {
            type: "array",
            items: { $ref: "#/components/schemas/ExecutionResult" },
          },
        },
      },
      CollectionSummary: {
        type: "object",
        required: [
          "id",
          "folderName",
          "collectionFile",
          "environmentFile",
          "fileName",
          "name",
          "requestCount",
          "status",
          "runId",
          "lastRunAt",
          "durationMs",
          "stats",
        ],
        properties: {
          id: { type: "string" },
          folderName: { type: "string" },
          folderPath: { type: "string" },
          collectionFile: { type: "string" },
          environmentFile: { type: "string" },
          fileName: { type: "string" },
          name: { type: "string" },
          requestCount: { type: "integer", minimum: 0 },
          environment: { type: "string", nullable: true },
          status: runStatusSchema,
          runId: { type: "string", nullable: true },
          lastRunAt: { type: "string", format: "date-time", nullable: true },
          durationMs: { type: "integer", nullable: true },
          stats: {
            allOf: [{ $ref: "#/components/schemas/RunStats" }],
            nullable: true,
          },
        },
      },
      DashboardCollection: {
        type: "object",
        required: [
          "id",
          "folderName",
          "collectionFile",
          "environmentFile",
          "status",
          "runId",
          "apiCount",
          "assertions",
          "tests",
          "lastRunAt",
          "durationMs",
          "actions",
        ],
        properties: {
          id: { type: "string" },
          folderName: { type: "string" },
          collectionFile: { type: "string" },
          environmentFile: { type: "string" },
          status: runStatusSchema,
          runId: { type: "string", nullable: true },
          apiCount: { type: "integer", minimum: 0 },
          assertions: { $ref: "#/components/schemas/AssertionSummary" },
          tests: {
            allOf: [{ $ref: "#/components/schemas/AssertionSummary" }],
            description:
              "One test per collection request. The total matches apiCount; failed and passed match the failed and passed request counts. Assertions are reported separately.",
          },
          lastRunAt: { type: "string", format: "date-time", nullable: true },
          durationMs: { type: "integer", nullable: true },
          actions: {
            type: "object",
            properties: {
              canRerun: { type: "boolean" },
              canViewHistory: { type: "boolean" },
            },
          },
        },
      },
      AssertionSummary: {
        type: "object",
        required: ["passed", "failed", "total"],
        properties: {
          passed: { type: "integer", minimum: 0 },
          failed: { type: "integer", minimum: 0 },
          total: { type: "integer", minimum: 0 },
        },
      },
      DashboardResponse: {
        type: "object",
        required: ["data", "meta"],
        properties: {
          data: {
            type: "object",
            required: ["kpis", "collections"],
            properties: {
              kpis: {
                type: "object",
                required: [
                  "totalCollections",
                  "passedCollections",
                  "failedCollections",
                  "totalApis",
                  "failedAssertions",
                ],
                properties: {
                  totalCollections: { type: "integer" },
                  passedCollections: { type: "integer" },
                  failedCollections: { type: "integer" },
                  totalApis: { type: "integer" },
                  failedAssertions: { type: "integer" },
                },
              },
              collections: {
                type: "array",
                items: { $ref: "#/components/schemas/DashboardCollection" },
              },
            },
          },
          meta: { $ref: "#/components/schemas/ResponseMeta" },
        },
      },
      SummaryResponse: {
        type: "object",
        properties: {
          totalCollections: { type: "integer" },
          passed: { type: "integer" },
          failed: { type: "integer" },
          running: { type: "integer" },
          totalApis: { type: "integer" },
          totalRequests: { type: "integer" },
          failedAssertions: { type: "integer" },
          averageResponseTimeMs: { type: "integer" },
        },
      },
      ResponseMeta: {
        type: "object",
        required: ["generatedAt"],
        properties: { generatedAt: { type: "string", format: "date-time" } },
      },
      SkippedFile: {
        type: "object",
        required: ["fileName", "reason"],
        properties: {
          folderName: { type: "string" },
          fileName: { type: "string" },
          reason: { type: "string" },
        },
      },
      RunStartResponse: {
        type: "object",
        required: ["data"],
        properties: {
          data: {
            type: "object",
            required: ["runId", "collectionId", "status", "statusUrl"],
            properties: {
              runId: { type: "string" },
              collectionId: { type: "string" },
              status: { type: "string", enum: ["running"] },
              statusUrl: { type: "string" },
            },
          },
        },
      },
      RunAllResponse: {
        type: "object",
        required: ["queued", "skipped"],
        properties: {
          queued: { type: "array", items: { type: "string" } },
          skipped: { type: "array", items: { type: "string" } },
        },
      },
      SelectedRunRequest: {
        type: "object",
        additionalProperties: false,
        required: ["collectionIds"],
        properties: {
          collectionIds: {
            type: "array",
            minItems: 1,
            maxItems: 50,
            uniqueItems: true,
            items: { type: "string", minLength: 1 },
            description:
              "Collection identifiers from GET /api/dashboard. IDs must be unique.",
          },
        },
      },
      SelectedRunResponse: {
        type: "object",
        required: ["data"],
        properties: {
          data: {
            type: "object",
            required: ["queued", "skipped"],
            properties: {
              queued: {
                type: "array",
                items: { $ref: "#/components/schemas/SelectedRunQueuedItem" },
              },
              skipped: {
                type: "array",
                items: { $ref: "#/components/schemas/SelectedRunSkippedItem" },
              },
            },
          },
        },
      },
      SelectedRunQueuedItem: {
        type: "object",
        required: ["collectionId", "runId", "status", "statusUrl"],
        properties: {
          collectionId: { type: "string" },
          runId: { type: "string" },
          status: { type: "string", enum: ["running"] },
          statusUrl: { type: "string", example: "/api/runs/run-uuid" },
        },
      },
      SelectedRunSkippedItem: {
        type: "object",
        required: ["collectionId", "reason"],
        properties: {
          collectionId: { type: "string" },
          reason: {
            type: "string",
            enum: [
              "collection_not_found",
              "invalid_collection",
              "already_running",
            ],
          },
        },
      },
      RunStatusResponse: {
        type: "object",
        required: ["data"],
        properties: {
          data: {
            type: "object",
            required: [
              "runId",
              "collectionId",
              "status",
              "startedAt",
              "completedAt",
              "durationMs",
              "stats",
            ],
            properties: {
              runId: { type: "string" },
              collectionId: { type: "string" },
              status: runStatusSchema,
              startedAt: {
                type: "string",
                format: "date-time",
                nullable: true,
              },
              completedAt: {
                type: "string",
                format: "date-time",
                nullable: true,
              },
              durationMs: { type: "integer", nullable: true },
              stats: {
                allOf: [{ $ref: "#/components/schemas/RunStats" }],
                nullable: true,
              },
            },
          },
        },
      },
      ResultResponse: {
        type: "object",
        required: ["result"],
        properties: {
          result: {
            allOf: [{ $ref: "#/components/schemas/RunResult" }],
            nullable: true,
          },
        },
      },
      HistoryRunSummary: {
        type: "object",
        required: [
          "runId",
          "runNumber",
          "startedAt",
          "completedAt",
          "durationMs",
          "status",
          "totalApis",
          "assertions",
          "avgLatencyMs",
          "reportUrl",
        ],
        properties: {
          runId: { type: "string" },
          runNumber: { type: "integer" },
          startedAt: { type: "string", format: "date-time" },
          completedAt: { type: "string", format: "date-time" },
          durationMs: { type: "integer" },
          status: runStatusSchema,
          totalApis: { type: "integer" },
          assertions: { $ref: "#/components/schemas/AssertionSummary" },
          avgLatencyMs: { type: "integer" },
          reportUrl: { type: "string" },
        },
      },
      HistoryResponse: {
        type: "object",
        required: ["data", "meta"],
        properties: {
          data: {
            type: "object",
            properties: {
              collection: {
                type: "object",
                required: ["id", "folderName"],
                properties: {
                  id: { type: "string" },
                  folderName: { type: "string" },
                },
              },
              runs: {
                type: "array",
                items: { $ref: "#/components/schemas/HistoryRunSummary" },
              },
            },
          },
          meta: {
            type: "object",
            required: ["limit", "count"],
            properties: {
              limit: { type: "integer" },
              count: { type: "integer" },
            },
          },
        },
      },
      EndpointResult: {
        type: "object",
        properties: {
          name: { type: "string" },
          method: { type: "string" },
          status: { type: "string", enum: ["passed", "failed"] },
          statusCode: { type: "integer", nullable: true },
          folder: { type: "string" },
          endpoint: { type: "string" },
          latencyMs: { type: "integer", nullable: true },
          payloadBytes: { type: "integer", nullable: true },
          assertions: { $ref: "#/components/schemas/AssertionSummary" },
          verifiedAssertions: {
            type: "array",
            items: { $ref: "#/components/schemas/AssertionResult" },
          },
        },
      },
      LatestRunResponse: {
        type: "object",
        required: ["data", "meta"],
        properties: {
          data: {
            type: "object",
            properties: {
              collection: {
                type: "object",
                properties: {
                  id: { type: "string" },
                  folderName: { type: "string" },
                },
              },
              run: {
                type: "object",
                properties: {
                  runId: { type: "string" },
                  status: runStatusSchema,
                  completedAt: { type: "string", format: "date-time" },
                  durationMs: { type: "integer" },
                  reportUrl: { type: "string" },
                },
              },
              kpis: {
                type: "object",
                properties: {
                  totalEndpoints: { type: "integer" },
                  passedRequests: { type: "integer" },
                  failedRequests: { type: "integer" },
                  avgResponseTimeMs: { type: "integer" },
                },
              },
              folders: {
                type: "array",
                items: {
                  type: "object",
                  properties: {
                    name: { type: "string" },
                    endpointCount: { type: "integer" },
                  },
                },
              },
              endpoints: {
                type: "array",
                items: { $ref: "#/components/schemas/EndpointResult" },
              },
            },
          },
          meta: {
            type: "object",
            properties: {
              filters: {
                type: "object",
                properties: {
                  search: { type: "string" },
                  status: { type: "string" },
                  method: { type: "string" },
                  folder: { type: "string" },
                },
              },
              totalItems: { type: "integer" },
              page: { type: "integer" },
              pageSize: { type: "integer" },
            },
          },
        },
      },
      AutoRerunSettings: {
        type: "object",
        additionalProperties: false,
        properties: {
          interval: {
            type: "string",
            enum: ["off", "15m", "1h", "24h"],
            example: "1h",
          },
          recipients: {
            type: "array",
            minItems: 1,
            items: { type: "string", format: "email" },
            example: ["qa-team@example.com"],
          },
          collectionIds: {
            type: "array",
            nullable: true,
            minItems: 1,
            maxItems: 50,
            uniqueItems: true,
            items: { type: "string", minLength: 1 },
            description:
              "IDs from GET /api/collections. Select one or more collections, or use null for all eligible collections. Omit to preserve the saved selection. Defaults to null for existing settings.",
            example: ["control-plane", "data-plane", "file-connect"],
          },
        },
      },
      AutoRerunSettingsResponse: {
        type: "object",
        required: ["data"],
        properties: {
          data: {
            type: "object",
            required: ["interval", "recipients", "collectionIds"],
            properties: {
              interval: { type: "string", enum: ["off", "15m", "1h", "24h"] },
              recipients: {
                type: "array",
                items: { type: "string", format: "email" },
              },
              collectionIds: {
                type: "array",
                nullable: true,
                items: { type: "string" },
                description:
                  "Selected scheduled collection IDs; null means all eligible collections.",
              },
            },
          },
        },
      },
    },
  },
} as const;
