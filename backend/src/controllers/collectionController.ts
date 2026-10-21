import type { Request, Response } from "express";
import type { Catalog } from "../catalog";
import { generateNewmanHtmlReport } from "../reportGenerator";
import { RunError, RunManager } from "../runner";
import type { ResultsStorePort } from "../store";
import { endpointRunQuerySchema, selectedRunRequestSchema } from "../validation";
import type { CollectionDescriptor, CollectionHistoryResponse, CollectionLatestRunResponse, CollectionSummary, RunStats } from "../types";

type CatalogLoader = () => Promise<Catalog>;
type CollectionParams = { id: string };
type HistoryReportParams = { id: string; runId: string };
type RunParams = { runId: string };

export class CollectionController {
  constructor(
    private readonly loadCatalog: CatalogLoader,
    private readonly store: ResultsStorePort,
    private readonly runner: RunManager,
    private readonly dashboardUrl: string
  ) {}

  private summaryForCollection(collection: CollectionDescriptor): CollectionSummary {
    const state = this.store.get(collection.id);
    return {
      id: collection.id,
      folderName: collection.folderName,
      folderPath: collection.folderPath,
      collectionFile: collection.collectionFile,
      environmentFile: collection.environmentFile,
      fileName: collection.fileName,
      name: collection.name,
      requestCount: collection.requestCount,
      environment: collection.environment,
      status: collection.parseError ? "error" : state.status,
      runId: state.runId ?? null,
      lastRunAt: state.lastRunAt,
      durationMs: state.durationMs,
      stats: state.stats
    };
  }

  private zeroStats(): RunStats {
    return { requests: { total: 0, failed: 0 }, assertions: { total: 0, failed: 0 }, tests: { total: 0, failed: 0 }, averageResponseTimeMs: 0 };
  }

  async list(_request: Request, response: Response): Promise<void> {
    await this.store.reload();
    const catalog = await this.loadCatalog();
    response.json(catalog.collections.map((collection) => this.summaryForCollection(collection)));
  }

  async skipped(_request: Request, response: Response): Promise<void> {
    response.json((await this.loadCatalog()).skipped);
  }

  async summary(_request: Request, response: Response): Promise<void> {
    await this.store.reload();
    const collections = (await this.loadCatalog()).collections.map((collection) => this.summaryForCollection(collection));
    const totals = collections.reduce(
      (aggregate, collection) => {
        const stats = collection.stats ?? this.zeroStats();
        aggregate.totalRequests += stats.requests.total;
        aggregate.failedAssertions += stats.assertions.failed;
        aggregate.responseTimeTotal += stats.averageResponseTimeMs * stats.requests.total;
        aggregate.responseTimeCount += stats.requests.total;
        aggregate[collection.status] += 1;
        return aggregate;
      },
      { totalRequests: 0, failedAssertions: 0, responseTimeTotal: 0, responseTimeCount: 0, idle: 0, running: 0, passed: 0, failed: 0, error: 0 }
    );
    response.json({
      totalCollections: collections.length,
      passed: totals.passed,
      failed: totals.failed,
      running: totals.running,
      totalApis: collections.reduce((total, collection) => total + collection.requestCount, 0),
      totalRequests: totals.totalRequests,
      failedAssertions: totals.failedAssertions,
      averageResponseTimeMs: totals.responseTimeCount ? Math.round(totals.responseTimeTotal / totals.responseTimeCount) : 0
    });
  }

  async getDashboardData(collectionIds: string[] | null = null) {
    await this.store.reload();
    const collections = (await this.loadCatalog()).collections
      .filter((collection) => collectionIds === null || collectionIds.includes(collection.id))
      .map((collection) => this.summaryForCollection(collection));
    const failedCollections = collections.filter((collection) => collection.status === "failed" || collection.status === "error");

    return {
      kpis: {
        totalCollections: collections.length,
        passedCollections: collections.filter((collection) => collection.status === "passed").length,
        failedCollections: failedCollections.length,
        totalApis: collections.reduce((total, collection) => total + collection.requestCount, 0),
        failedAssertions: collections.reduce((total, collection) => total + (collection.stats?.assertions.failed ?? 0), 0)
      },
      collections: collections.map((collection) => ({
        id: collection.id,
        folderName: collection.folderName,
        collectionFile: collection.collectionFile,
        environmentFile: collection.environmentFile,
        status: collection.status,
        runId: collection.runId,
        apiCount: collection.requestCount,
        assertions: {
          passed: (collection.stats?.assertions.total ?? 0) - (collection.stats?.assertions.failed ?? 0),
          failed: collection.stats?.assertions.failed ?? 0,
          total: collection.stats?.assertions.total ?? 0
        },
        // Executed requests from the last run; requests that never ran are not counted as passed.
        tests: {
          passed: Math.max(0, (collection.stats?.tests.total ?? 0) - (collection.stats?.tests.failed ?? 0)),
          failed: collection.stats?.tests.failed ?? 0,
          total: collection.stats?.tests.total ?? 0
        },
        lastRunAt: collection.lastRunAt,
        durationMs: collection.durationMs,
        actions: { canRerun: true, canViewHistory: true }
      }))
    };
  }

  async dashboard(_request: Request, response: Response): Promise<void> {
    response.json({ data: await this.getDashboardData(), meta: { generatedAt: new Date().toISOString() } });
  }

  async result(request: Request<CollectionParams>, response: Response): Promise<void> {
    await this.store.reload();
    const catalog = await this.loadCatalog();
    if (!catalog.collections.some((collection) => collection.id === request.params.id)) {
      response.status(404).json({ error: { code: "COLLECTION_NOT_FOUND", message: "Collection not found." } });
      return;
    }
    response.json({ result: this.store.get(request.params.id).result });
  }

  async latestRun(request: Request<CollectionParams>, response: Response): Promise<void> {
    const query = endpointRunQuerySchema.safeParse(request.query);
    if (!query.success) {
      response.status(400).json({
        error: {
          code: "VALIDATION_ERROR",
          message: "Invalid endpoint filters.",
          details: query.error.flatten()
        }
      });
      return;
    }

    await this.store.reload();
    const catalog = await this.loadCatalog();
    const descriptor = catalog.collections.find((collection) => collection.id === request.params.id);
    if (!descriptor) {
      response.status(404).json({ error: { code: "COLLECTION_NOT_FOUND", message: "Collection not found." } });
      return;
    }

    const state = this.store.get(request.params.id);
    const run = state.history?.[0];
    if (!run) {
      response.status(404).json({ error: { code: "RUN_NOT_FOUND", message: "No completed run found." } });
      return;
    }

    const normalizedSearch = query.data.search.toLowerCase();
    const normalizedFolder = query.data.folder;
    const allExecutions = run.executions;
    const filteredExecutions = allExecutions.filter((execution) => {
      const status = execution.failed ? "failed" : "passed";
      const method = execution.method.toUpperCase();
      const folder = execution.folder ?? "Root Requests";
      return (!normalizedSearch || execution.name.toLowerCase().includes(normalizedSearch))
        && (query.data.status === "all" || query.data.status === status)
        && (query.data.method === "all" || query.data.method === method)
        && (normalizedFolder === "all" || normalizedFolder === folder);
    });

    const folderCounts = new Map<string, number>();
    for (const execution of allExecutions) {
      const folder = execution.folder ?? "Root Requests";
      folderCounts.set(folder, (folderCounts.get(folder) ?? 0) + 1);
    }

    const start = (query.data.page - 1) * query.data.pageSize;
    const endpoints = filteredExecutions.slice(start, start + query.data.pageSize).map((execution) => {
      const totalAssertions = execution.assertions.length;
      const failedAssertions = execution.assertions.filter((assertion) => !assertion.passed).length;
      return {
        name: execution.name,
        method: execution.method.toUpperCase(),
        status: execution.failed ? "failed" as const : "passed" as const,
        statusCode: execution.statusCode,
        folder: execution.folder ?? "Root Requests",
        endpoint: execution.url,
        latencyMs: execution.responseTimeMs,
        payloadBytes: execution.responseSize,
        assertions: {
          passed: totalAssertions - failedAssertions,
          failed: failedAssertions,
          total: totalAssertions
        },
        verifiedAssertions: execution.assertions
      };
    });

    const body: CollectionLatestRunResponse = {
      data: {
        collection: { id: descriptor.id, folderName: descriptor.folderName },
        run: {
          runId: run.runId,
          status: run.status,
          completedAt: run.completedAt,
          durationMs: run.durationMs,
          reportUrl: `/api/collections/${encodeURIComponent(descriptor.id)}/history/${encodeURIComponent(run.runId)}/report`
        },
        kpis: {
          totalEndpoints: run.stats.requests.total,
          passedRequests: run.stats.requests.total - run.stats.requests.failed,
          failedRequests: run.stats.requests.failed,
          avgResponseTimeMs: run.stats.averageResponseTimeMs
        },
        folders: [...folderCounts.entries()]
          .map(([name, endpointCount]) => ({ name, endpointCount }))
          .sort((a, b) => a.name.localeCompare(b.name)),
        endpoints
      },
      meta: {
        filters: {
          search: query.data.search,
          status: query.data.status,
          method: query.data.method,
          folder: query.data.folder
        },
        totalItems: filteredExecutions.length,
        page: query.data.page,
        pageSize: query.data.pageSize
      }
    };

    response.json(body);
  }

  async history(request: Request<CollectionParams>, response: Response): Promise<void> {
    await this.store.reload();
    const catalog = await this.loadCatalog();
    const descriptor = catalog.collections.find((collection) => collection.id === request.params.id);
    if (!descriptor) {
      response.status(404).json({ error: { code: "COLLECTION_NOT_FOUND", message: "Collection not found." } });
      return;
    }

    const history = (this.store.get(request.params.id).history ?? []).slice(0, 5);
    const body: CollectionHistoryResponse = {
      data: {
        collection: { id: descriptor.id, folderName: descriptor.folderName },
        runs: history.map((run, index) => {
          const totalAssertions = run.stats.assertions.total;
          const failedAssertions = run.stats.assertions.failed;
          return {
            runId: run.runId,
            runNumber: history.length - index,
            startedAt: run.startedAt,
            completedAt: run.completedAt,
            durationMs: run.durationMs,
            status: run.status,
            totalApis: run.stats.requests.total,
            assertions: {
              passed: Math.max(0, totalAssertions - failedAssertions),
              failed: failedAssertions,
              total: totalAssertions
            },
            avgLatencyMs: run.stats.averageResponseTimeMs,
            reportUrl: `/api/collections/${encodeURIComponent(descriptor.id)}/history/${encodeURIComponent(run.runId)}/report`
          };
        })
      },
      meta: { limit: 5, count: history.length }
    };

    response.json(body);
  }

  async historyReport(request: Request<HistoryReportParams>, response: Response): Promise<void> {
    await this.store.reload();
    const catalog = await this.loadCatalog();
    const descriptor = catalog.collections.find((collection) => collection.id === request.params.id);
    if (!descriptor) {
      response.status(404).send("Collection not found.");
      return;
    }
    const state = this.store.get(request.params.id);
    const run = (state.history ?? []).find((history) => history.runId === request.params.runId);
    if (!run) {
      response.status(404).json({ error: { code: "RUN_NOT_FOUND", message: "Run not found." } });
      return;
    }
    const html = generateNewmanHtmlReport(descriptor, state, run, this.dashboardUrl);
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.send(html);
  }

  async runStatus(request: Request<RunParams>, response: Response): Promise<void> {
    await this.store.reload();
    const found = this.store.findRun(request.params.runId);
    if (!found) {
      response.status(404).json({ error: { code: "RUN_NOT_FOUND", message: "Run not found." } });
      return;
    }

    const { state, run } = found;
    const status = run?.status ?? state.status;
    response.json({
      data: {
        runId: request.params.runId,
        collectionId: found.collectionId,
        status,
        startedAt: run?.startedAt ?? state.runStartedAt ?? null,
        completedAt: run?.completedAt ?? (status === "running" ? null : state.lastRunAt),
        durationMs: run?.durationMs ?? (status === "running" ? null : state.durationMs),
        stats: run?.stats ?? (status === "running" ? null : state.stats)
      }
    });
  }

  async report(request: Request<CollectionParams>, response: Response): Promise<void> {
    await this.store.reload();
    const catalog = await this.loadCatalog();
    const descriptor = catalog.collections.find((collection) => collection.id === request.params.id);
    if (!descriptor) {
      response.status(404).send("Collection not found.");
      return;
    }
    const state = this.store.get(request.params.id);
    const html = generateNewmanHtmlReport(descriptor, state, null, this.dashboardUrl);
    response.setHeader("Content-Type", "text/html; charset=utf-8");
    response.send(html);
  }

  async runOne(request: Request<CollectionParams>, response: Response): Promise<void> {
    try {
      const runId = await this.runner.startOne(request.params.id);
      response.status(202).json({
        data: {
          runId,
          collectionId: request.params.id,
          status: "running",
          statusUrl: `/api/runs/${encodeURIComponent(runId)}`
        }
      });
    } catch (error) {
      if (error instanceof RunError) {
        response.status(error.status).json({ error: { code: error.code, message: error.message } });
        return;
      }
      throw error;
    }
  }

  async runAll(_request: Request, response: Response): Promise<void> {
    const outcome = await this.runner.startAll();
    response.status(202).json(outcome);
  }

  async runSelected(request: Request, response: Response): Promise<void> {
    const parsed = selectedRunRequestSchema.safeParse(request.body);
    if (!parsed.success) {
      response.status(400).json({
        error: {
          code: "VALIDATION_ERROR",
          message: "Provide between 1 and 50 unique collection IDs.",
          details: parsed.error.flatten()
        }
      });
      return;
    }

    const outcome = await this.runner.startSelected(parsed.data.collectionIds);
    response.status(202).json({ data: outcome });
  }
}
