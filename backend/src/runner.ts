import newman from "newman";
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdir, readFile, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";
import { flattenRequests, type Catalog } from "./catalog";
import type { ResultsStorePort } from "./store";
import type {
  AssertionResult,
  CollectionDescriptor,
  ExecutionResult,
  PostRunManifest,
  PostRunStep,
  RunResult,
  SelectedRunOutcome,
} from "./types";

type CatalogLoader = () => Promise<Catalog>;

export class RunError extends Error {
  constructor(
    public readonly status: number,
    public readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function redactableValues(
  environment: Record<string, unknown> | null,
): string[] {
  if (!environment || !Array.isArray(environment.values)) return [];
  return environment.values
    .filter(isRecord)
    .flatMap((entry) => {
      const key = typeof entry.key === "string" ? entry.key : "";
      const value = typeof entry.value === "string" ? entry.value : "";
      const isSecret =
        entry.type === "secret" || /token|secret|key|password|auth/i.test(key);
      const isBaseUrl = /^https?:\/\//i.test(value);
      return isSecret && value.length >= 6 && !isBaseUrl ? [value] : [];
    })
    .sort((a, b) => b.length - a.length);
}

function redact(value: string | null, secrets: string[]): string | null {
  if (value === null) return null;
  return secrets.reduce(
    (result, secret) => result.split(secret).join("[REDACTED]"),
    value,
  );
}

async function loadEnvironment(
  collectionsDir: string,
  folderPath: string,
  environmentFile: string | null,
): Promise<Record<string, unknown> | undefined> {
  if (!environmentFile) return undefined;
  const parsed: unknown = JSON.parse(
    await readFile(
      path.join(collectionsDir, folderPath, environmentFile),
      "utf8",
    ),
  );
  if (!isRecord(parsed) || !Array.isArray(parsed.values))
    throw new Error("Mapped environment is invalid.");
  return structuredClone(parsed);
}

function environmentValue(
  environment: Record<string, unknown> | undefined,
  key: string,
): string | undefined {
  if (!environment || !Array.isArray(environment.values)) return undefined;
  const entry = environment.values.find(
    (value) => isRecord(value) && value.key === key,
  );
  return isRecord(entry) && typeof entry.value === "string"
    ? entry.value
    : undefined;
}

function setEnvironmentValue(
  environment: Record<string, unknown> | undefined,
  key: string,
  value: string,
): void {
  if (!environment || !Array.isArray(environment.values)) return;
  const entry = environment.values.find(
    (candidate) => isRecord(candidate) && candidate.key === key,
  );
  if (isRecord(entry)) entry.value = value;
}

function passwordStatePath(collectionsDir: string): string {
  const configuredPath = process.env.CONTROL_PLANE_PASSWORD_FILE;
  return path.resolve(
    configuredPath ||
      path.join(collectionsDir, "..", "data", "control-plane-password.json"),
  );
}

async function writeControlPlanePassword(
  filePath: string,
  passwords: { consumer_pass: string; new_password_value: string }, // gitleaks:allow
): Promise<void> {
  await mkdir(path.dirname(filePath), { recursive: true });
  const temporaryPath = `${filePath}.${process.pid}.${Date.now()}.tmp`;
  await writeFile(
    temporaryPath,
    `${JSON.stringify(passwords, null, 2)}\n`,
    { encoding: "utf8", mode: 0o600 },
  );
  await rename(temporaryPath, filePath);
}

type ControlPlanePasswords = {
  consumer_pass: string | undefined; // gitleaks:allow
  new_password_value: string | undefined;
};

async function loadControlPlanePasswords(
  filePath: string,
  fallback: ControlPlanePasswords,
): Promise<ControlPlanePasswords> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(await readFile(filePath, "utf8"));
  } catch (error) {
    if (!isRecord(error) || error.code !== "ENOENT") throw error;
    await writeControlPlanePassword(filePath, {
      consumer_pass: fallback.consumer_pass ?? "", // gitleaks:allow
      new_password_value: fallback.new_password_value ?? "",
    });
    return fallback;
  }
  if (
    !isRecord(parsed) ||
    (parsed.consumer_pass !== undefined &&
      typeof parsed.consumer_pass !== "string") ||
    (parsed.new_password_value !== undefined &&
      typeof parsed.new_password_value !== "string")
  ) {
    throw new Error(
      `Control Plane password file is invalid: password fields must be strings in ${filePath}.`,
    );
  }
  return {
    consumer_pass: parsed.consumer_pass || fallback.consumer_pass, // gitleaks:allow
    new_password_value: parsed.new_password_value || fallback.new_password_value,
  };
}

export function runNewman(
  collection: Record<string, unknown>,
  environment: Record<string, unknown> | undefined,
  workingDir: string,
): Promise<any> {
  return new Promise((resolve, reject) => {
    let finalEnvironment: Record<string, unknown> | undefined;
    let finalCollection: Record<string, unknown> | undefined;
    // pm.sendRequest() calls in scripts fire "request" events for the same item. Newman then pushes
    // that item's execution object again and overwrites its request/response with the helper's.
    // Remember each item's own request so the duplicates can be dropped and the data restored.
    const itemIds = new Map<string, unknown>();
    const ownRequests = new Map<
      string,
      {
        id: unknown;
        request: unknown;
        response: unknown;
        requestError: unknown;
      }
    >();
    const run = newman.run(
      {
        collection,
        environment,
        workingDir,
        reporters: [],
        timeoutRequest: 30_000,
        exports: {
          environment: (error: Error | null, value: unknown) => {
            if (!error && isRecord(value)) finalEnvironment = value;
          },
          collection: (error: Error | null, value: unknown) => {
            if (!error && isRecord(value)) finalCollection = value;
          },
        },
      },
      (error: Error | null, summary: unknown) => {
        if (error) return reject(error);
        if (isRecord(summary)) {
          if (finalEnvironment)
            summary.environment = { toJSON: () => finalEnvironment };
          if (finalCollection)
            summary.collection = { toJSON: () => finalCollection };
          if (isRecord(summary.run) && Array.isArray(summary.run.executions)) {
            // Newman can append the same execution object more than once for
            // pm.sendRequest() helpers. Deduplicate only for report counts and
            // retain Newman’s original request, response, and assertion data.
            summary.run.executions = [...new Set(summary.run.executions)];
          }
        }
        resolve(summary);
      },
    );
    run.on("beforeItem", (_error, args) => {
      if (args?.cursor?.ref) itemIds.set(args.cursor.ref, args.item?.id);
    });
    run.on("request", (error, args) => {
      const ref = args?.cursor?.ref;
      if (!ref || ownRequests.has(ref) || args.item?.id !== itemIds.get(ref))
        return;
      ownRequests.set(ref, {
        id: args.item?.id,
        request: args.request,
        response: args.response,
        requestError: error ?? undefined,
      });
    });
  });
}

function environmentValues(
  environment: Record<string, unknown> | undefined,
  collection: Record<string, unknown> | undefined,
): Record<string, string> {
  const result: Record<string, string> = {};
  if (environment && Array.isArray(environment.values))
    for (const entry of environment.values.filter(isRecord)) {
      if (typeof entry.key === "string" && entry.enabled !== false)
        result[`env.${entry.key}`] =
          entry.value === undefined || entry.value === null
            ? ""
            : String(entry.value);
    }
  if (Array.isArray(collection?.variable)) {
    for (const entry of collection.variable.filter(isRecord)) {
      if (typeof entry.key === "string" && entry.enabled !== false)
        result[`collection.${entry.key}`] =
          entry.value === undefined || entry.value === null
            ? ""
            : String(entry.value);
    }
  }
  return result;
}

function resolveTemplate(
  value: string,
  values: Record<string, string>,
  stepName: string,
): string {
  return value
    .replace(
      /\{\{\s*(env\.)?([A-Za-z0-9_.-]+)\s*\}\}/g,
      (_match, prefix: string | undefined, key: string) => {
        const lookupKey = prefix ? `env.${key}` : key;
        const resolved = values[lookupKey];
        if (resolved === undefined || resolved.length === 0)
          throw new Error(
            `Post-run step "${stepName}" requires missing ${prefix ? "environment" : "exported"} value "${key}".`,
          );
        return resolved;
      },
    )
    .replace(/\{\{[^{}]*\}\}/g, (placeholder) => {
      throw new Error(
        `Post-run step "${stepName}" contains unsupported template "${placeholder}".`,
      );
    });
}

function runPostRunStep(
  step: PostRunStep,
  workingDir: string,
  values: Record<string, string>,
  secrets: string[],
  logger: { info(message: string, metadata?: Record<string, unknown>): void },
): Promise<void> {
  const args = step.args.map((argument) =>
    resolveTemplate(argument, values, step.name),
  );
  const env = {
    ...process.env,
    ...Object.fromEntries(
      Object.entries(step.env ?? {}).map(([key, value]) => [
        key,
        resolveTemplate(value, values, step.name),
      ]),
    ),
  };
  const references = [...step.args, ...Object.values(step.env ?? {})].flatMap(
    (argument) => [
      ...argument.matchAll(/\{\{\s*(env\.)?([A-Za-z0-9_.-]+)\s*\}\}/g),
    ],
  );
  const loggedValues = new Set<string>();
  for (const reference of references) {
    const variableName = reference[2];
    const lookupKey = reference[1] ? `env.${variableName}` : variableName;
    if (loggedValues.has(lookupKey)) continue;
    loggedValues.add(lookupKey);
    const resolvedValue = values[lookupKey];
    logger.info("Resolved post-run input", {
      step: step.name,
      variable: variableName,
      source: reference[1] ? "environment" : "manifest export",
      present: resolvedValue !== undefined && resolvedValue.length > 0,
      length: resolvedValue?.length ?? 0,
    });
  }
  return new Promise((resolve, reject) => {
    const useShell = step.shell ?? process.platform === "win32";
    const commandLine = useShell
      ? [step.command, ...args]
          .map((part) => `"${part.replace(/"/g, '\\"')}"`)
          .join(" ")
      : step.command;
    const child = spawn(commandLine, useShell ? [] : args, {
      cwd: workingDir,
      env,
      shell: useShell,
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      const metadata = { step: step.name };
      const safeStdout = redact(stdout.trim(), secrets);
      const safeStderr = redact(stderr.trim(), secrets);
      if (code === 0) {
        logger.info("Post-run cleanup step completed", metadata);
        if (safeStdout)
          logger.info("Post-run cleanup output", {
            ...metadata,
            output: safeStdout,
          });
        if (safeStderr)
          logger.info("Post-run cleanup stderr", {
            ...metadata,
            output: safeStderr,
          });
        return resolve();
      }
      const detail = safeStderr;
      reject(
        new Error(
          `Post-run step "${step.name}" exited with ${signal ? `signal ${signal}` : `code ${code}`}.${detail ? ` ${detail}` : ""}`,
        ),
      );
    });
  });
}

function runCommand(
  command: string,
  args: string[],
  cwd: string,
): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    // Spawn executables directly so Windows paths containing spaces are passed
    // as a single executable/argument instead of being split by cmd.exe.
    const child = spawn(command, args, {
      cwd,
      shell: false,
      windowsHide: true,
    });
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
    });
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", reject);
    child.on("close", (code, signal) => {
      if (code === 0) return resolve({ stdout, stderr });
      reject(
        new Error(
          `${command} ${args.join(" ")} exited with ${signal ? `signal ${signal}` : `code ${code}`}.${stderr.trim() ? ` ${stderr.trim()}` : ""}`,
        ),
      );
    });
  });
}

async function preparePythonEnvironment(
  workingDir: string,
  collectionId: string,
  requirementsFile: string,
  logger: { info(message: string, metadata?: Record<string, unknown>): void },
): Promise<string> {
  if (
    path.isAbsolute(requirementsFile) ||
    requirementsFile.split(/[\\/]/).includes("..")
  ) {
    throw new Error(
      `Python requirements file must stay inside the collection folder: ${requirementsFile}`,
    );
  }
  const requirementsPath = path.join(workingDir, requirementsFile);
  const requirements = await readFile(requirementsPath);
  const requirementsHash = createHash("sha256")
    .update(requirements)
    .digest("hex");
  const configuredRoot = process.env.POST_RUN_VENV_ROOT;
  const environmentRoot = path.resolve(
    configuredRoot || path.resolve(workingDir, "..", "..", ".post-run-venvs"),
  );
  const environmentDir = path.join(environmentRoot, collectionId);
  const pythonPath = path.join(
    environmentDir,
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
  const markerPath = path.join(environmentDir, ".requirements.sha256");

  try {
    await stat(pythonPath);
  } catch {
    if (configuredRoot) {
      throw new Error(
        `Prebuilt Python environment is missing for collection "${collectionId}" at ${environmentDir}. Rebuild the deployment image.`,
      );
    }
    logger.info("Creating isolated Python environment", {
      directory: environmentDir,
    });
    await mkdir(environmentRoot, { recursive: true });
    await runCommand(
      process.platform === "win32" ? "python" : "python3",
      ["-m", "venv", environmentDir],
      workingDir,
    );
  }

  let installedHash = "";
  try {
    installedHash = (await readFile(markerPath, "utf8")).trim();
  } catch {
    /* install on first use */
  }
  if (installedHash !== requirementsHash) {
    if (configuredRoot) {
      throw new Error(
        `Python environment dependencies are out of date for collection "${collectionId}". Rebuild the deployment image.`,
      );
    }
    logger.info("Installing Python cleanup dependencies", {
      requirements: requirementsFile,
    });
    const install = await runCommand(
      pythonPath,
      ["-m", "pip", "install", "-r", requirementsPath],
      workingDir,
    );
    if (install.stdout.trim())
      logger.info("Python dependency installation output", {
        output: install.stdout.trim(),
      });
    if (install.stderr.trim())
      logger.info("Python dependency installation details", {
        output: install.stderr.trim(),
      });
    await writeFile(markerPath, requirementsHash, "utf8");
  }
  return pythonPath;
}

async function runPostRunManifest(
  collectionsDir: string,
  descriptor: CollectionDescriptor,
  values: Record<string, string>,
  secrets: string[],
  logger: { info(message: string, metadata?: Record<string, unknown>): void },
): Promise<void> {
  if (!descriptor.postRunFile) return;
  const manifestPath = path.join(
    collectionsDir,
    descriptor.folderPath,
    descriptor.postRunFile,
  );
  const parsed: unknown = JSON.parse(await readFile(manifestPath, "utf8"));
  if (!isRecord(parsed) || !Array.isArray(parsed.steps))
    throw new Error("Post-run manifest is invalid.");
  const manifest = parsed as unknown as PostRunManifest;
  for (const [outputName, source] of Object.entries(manifest.export ?? {})) {
    const match = source.match(/^(environment|collection)\.([A-Za-z0-9_.-]+)$/);
    if (!match)
      throw new Error(
        `Invalid post-run export source "${source}" for "${outputName}".`,
      );
    const scope = match[1] === "environment" ? "env" : "collection";
    const value = values[`${scope}.${match[2]}`];
    if (value === undefined || value === null || String(value).length === 0) {
      throw new Error(
        `Post-run export "${outputName}" requires ${match[1]} value "${match[2]}".`,
      );
    }
    values[outputName] = String(value);
  }
  const workingDir = path.join(collectionsDir, descriptor.folderPath);
  const pythonPath = manifest.python
    ? await preparePythonEnvironment(
        workingDir,
        descriptor.id,
        manifest.python.requirements,
        logger,
      )
    : null;
  for (const rawStep of parsed.steps) {
    if (
      !isRecord(rawStep) ||
      typeof rawStep.name !== "string" ||
      typeof rawStep.command !== "string" ||
      !Array.isArray(rawStep.args)
    ) {
      throw new Error("Post-run manifest contains an invalid step.");
    }
    const step = rawStep as unknown as PostRunStep;
    const runnableStep =
      pythonPath && step.command === "python"
        ? { ...step, command: pythonPath, shell: false }
        : step;
    logger.info("Starting post-run cleanup step", {
      collectionId: descriptor.id,
      step: step.name,
    });
    await runPostRunStep(runnableStep, workingDir, values, secrets, logger);
  }
}

// Folder path from the request's own parent groups. Positions in the collection can't be
// used: requests skipped or repeated by postman.setNextRequest() shift every later index.
function folderOf(item: any): string | null | undefined {
  if (typeof item?.parent !== "function") return undefined;
  const names: string[] = [];
  let group = item.parent();
  while (group && typeof group.parent === "function" && group.parent()) {
    names.unshift(String(group.name));
    group = group.parent();
  }
  return names.length ? names.join(" / ") : null;
}

// Credentials are never stored in results: these headers keep only their scheme, and body
// fields whose names look sensitive are masked. Known secret values are redacted everywhere.
const CREDENTIAL_HEADERS = /^(authorization|proxy-authorization|cookie|x-api-key)$/i;
const SENSITIVE_FIELD = /pass|secret|token|api[-_]?key/i;
const MAX_BODY_CHARS = 20_000;

function sentHeaders(request: any, secrets: string[]): Array<{ key: string; value: string }> {
  const headers = typeof request?.headers?.all === "function" ? request.headers.all() : [];
  return headers
    .filter((header: any) => header && !header.disabled)
    .map((header: any) => {
      const key = String(header.key);
      const value = String(header.value ?? "");
      if (!CREDENTIAL_HEADERS.test(key)) return { key, value: redact(value, secrets) ?? "" };
      const scheme = value.match(/^(Bearer|Basic|Digest)\s/i)?.[1];
      return { key, value: scheme ? `${scheme} [REDACTED]` : "[REDACTED]" };
    });
}

function maskSensitiveFields(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(maskSensitiveFields);
  if (value === null || typeof value !== "object") return value;
  return Object.fromEntries(
    Object.entries(value).map(([key, field]) => [key, SENSITIVE_FIELD.test(key) ? "[REDACTED]" : maskSensitiveFields(field)]),
  );
}

function sentBody(request: any, secrets: string[]): string | null {
  const body = request?.body;
  if (!body || body.disabled || !body.mode) return null;
  const fields = (list: any) =>
    (typeof list?.all === "function" ? list.all() : [])
      .filter((field: any) => !field.disabled)
      .map((field: any) => {
        const value = SENSITIVE_FIELD.test(String(field.key)) ? "[REDACTED]" : field.type === "file" ? `@${field.src ?? "file"}` : (field.value ?? "");
        return `${field.key}=${value}`;
      })
      .join("&");

  let text: string;
  if (body.mode === "raw") {
    text = String(body.raw ?? "");
    try {
      const masked = maskSensitiveFields(JSON.parse(text));
      if (JSON.stringify(masked) !== JSON.stringify(JSON.parse(text))) text = JSON.stringify(masked, null, 2);
    } catch {
      /* not JSON: keep the raw text */
    }
  } else if (body.mode === "urlencoded") text = fields(body.urlencoded);
  else if (body.mode === "formdata") text = fields(body.formdata);
  else if (body.mode === "graphql") text = JSON.stringify(body.graphql ?? {});
  else if (body.mode === "file") text = `@${body.file?.src ?? "file"}`;
  else return null;

  if (!text) return null;
  const safe = redact(text, secrets) ?? "";
  return safe.length > MAX_BODY_CHARS ? `${safe.slice(0, MAX_BODY_CHARS)}… [truncated]` : safe;
}

// postman.setNextRequest() can run one request several times (polling, cleanup loops). A test
// is one collection request, so keep each request's final run, in the order requests first ran.
function finalRunPerRequest(executions: any[]): any[] {
  const byRequest = new Map<unknown, any>();
  for (const execution of executions)
    byRequest.set(execution.item?.id ?? execution.item ?? execution, execution);
  return [...byRequest.values()];
}

export function normaliseResult(
  summary: any,
  collection: Record<string, unknown>,
  secrets: string[],
  startedAt: Date,
): RunResult {
  const requestMetadata = flattenRequests(collection);
  const executions: ExecutionResult[] = finalRunPerRequest(
    summary?.run?.executions ?? [],
  ).map(
    (execution: any) => {
      const itemName = String(execution.item?.name ?? "Unnamed request");
      const folder =
        folderOf(execution.item) ??
        requestMetadata.find((request) => request.name === itemName)?.folder ??
        null;
      const statusCode =
        typeof execution.response?.code === "number"
          ? execution.response.code
          : null;

      // Requests without test scripts get one status check, expecting the code in the request
      // name (e.g. "Get Item 404"), or 200 when the name has none.
      const matchExpectedCode = itemName.match(
        /\b(200|201|202|204|400|401|403|404|415)\b/,
      );
      const expectedStatus = matchExpectedCode
        ? parseInt(matchExpectedCode[1], 10)
        : 200;

      let responseSize: number | null = null;
      try {
        responseSize = execution.response?.size?.().body ?? null;
      } catch {
        responseSize = null;
      }

      const requestError = redact(
        execution.requestError?.message ?? null,
        secrets,
      );

      // Newman's verdict is authoritative: an assertion passes only if its test script passed.
      const assertions: AssertionResult[] = (execution.assertions ?? []).map(
        (assertion: any) => ({
          name: String(assertion.assertion ?? "Unnamed assertion"),
          passed: !assertion.error && !requestError,
          errorMessage: redact(assertion.error?.message ?? null, secrets),
        }),
      );

      if (assertions.length === 0 && statusCode !== null) {
        assertions.push({
          name: `Status code is ${statusCode}`,
          passed: statusCode === expectedStatus || statusCode < 400,
          errorMessage:
            statusCode !== expectedStatus && statusCode >= 400
              ? `Expected ${expectedStatus} but received ${statusCode}`
              : null,
        });
      }

      const hasFailedAssertions = assertions.some(
        (assertion) => !assertion.passed,
      );
      const failed = Boolean(requestError) || hasFailedAssertions;
      const request = execution.request ?? {};

      return {
        folder,
        name: itemName,
        method: String(request.method ?? "GET"),
        url: redact(request.url?.toString?.() ?? "", secrets) ?? "",
        statusCode,
        responseTimeMs:
          typeof execution.response?.responseTime === "number"
            ? execution.response.responseTime
            : null,
        responseSize,
        assertions,
        requestError,
        failed,
        requestHeaders: sentHeaders(request, secrets),
        requestBody: sentBody(request, secrets),
      };
    },
  );

  const failedExecutionsCount = executions.filter(
    (execution) => execution.failed,
  ).length;
  // Collection requests that never ran, e.g. jumped over by postman.setNextRequest().
  const ran = new Set(executions.map((execution) => `${execution.folder}\u0000${execution.name}`));
  const skippedRequests = requestMetadata
    .filter((request) => !ran.has(`${request.folder}\u0000${request.name}`))
    .map((request) => ({ folder: request.folder, name: request.name, method: request.method, url: redact(request.url, secrets) ?? "" }));
  const allAssertions = executions.flatMap((execution) => execution.assertions);
  const responseTimes = executions
    .map((execution) => execution.responseTimeMs)
    .filter((time): time is number => time !== null);
  // One test is one executed collection request (see TestStats in the API docs). Requests
  // that never ran (skipped by setNextRequest) are neither passed nor failed, so they are
  // not counted; otherwise "passed = total - failed" would report them as passed.
  const completedAt = new Date();

  return {
    startedAt: startedAt.toISOString(),
    completedAt: completedAt.toISOString(),
    durationMs: completedAt.getTime() - startedAt.getTime(),
    stats: {
      requests: {
        total: executions.length,
        failed: failedExecutionsCount,
      },
      assertions: {
        total: allAssertions.length,
        failed: allAssertions.filter((assertion) => !assertion.passed).length,
      },
      // A test is one request with all its checks. Newman's own stats.tests only counts test
      // scripts that ran and fails them only on script errors, so it always showed e.g. 535/535.
      tests: { total: executions.length, failed: failedExecutionsCount },
      averageResponseTimeMs: responseTimes.length
        ? Math.round(
            responseTimes.reduce((sum, time) => sum + time, 0) /
              responseTimes.length,
          )
        : 0,
    },
    executions,
    skippedRequests,
  };
}

export class RunManager {
  private queue: { descriptor: CollectionDescriptor; done: () => void }[] = [];
  private activeRuns = 0;
  private reservedCollections = new Set<string>();
  private inFlight = new Map<string, Promise<void>>();

  constructor(
    private readonly collectionsDir: string,
    private readonly loadCatalog: CatalogLoader,
    private readonly store: ResultsStorePort,
    private readonly logger: {
      info(message: string, metadata?: Record<string, unknown>): void;
      error(message: string, metadata?: Record<string, unknown>): void;
    },
  ) {}

  async startOne(id: string): Promise<string> {
    const catalog = await this.loadCatalog();
    const outcome = await this.queueSelected([id], catalog);
    const queued = outcome.queued[0];
    if (queued) return queued.runId;

    const skipped = outcome.skipped[0];
    if (skipped?.reason === "collection_not_found") {
      throw new RunError(404, "COLLECTION_NOT_FOUND", "Collection not found.");
    }
    if (skipped?.reason === "already_running") {
      throw new RunError(
        409,
        "COLLECTION_RUNNING",
        "Collection is already running.",
      );
    }
    if (skipped?.reason === "invalid_collection")
      await this.store.complete(id, "error", null);
    throw new RunError(
      422,
      "COLLECTION_UNPARSEABLE",
      "Collection cannot be run because its JSON is invalid.",
    );
  }

  async startSelected(ids: string[]): Promise<SelectedRunOutcome> {
    const catalog = await this.loadCatalog();
    const outcome = await this.queueSelected(ids, catalog);
    return { queued: outcome.queued, skipped: outcome.skipped };
  }

  async startAll(): Promise<{ queued: string[]; skipped: string[] }> {
    const { queued, skipped } = await this.queueAll();
    return { queued, skipped };
  }

  async runAllAndWait(collectionIds: string[] | null = null): Promise<{
    queued: string[];
    skipped: string[];
    waitedFor: string[];
  }> {
    const { queued, skipped, finished } = await this.queueAll(collectionIds);
    // Collections skipped because they were already running (e.g. a manual run) are
    // awaited too, so the report never mixes fresh results with in-progress ones.
    const waitedFor = skipped.filter((id) => this.inFlight.has(id));
    await Promise.all([
      finished,
      ...waitedFor.map((id) => this.inFlight.get(id)),
    ]);
    return { queued, skipped, waitedFor };
  }

  private async queueAll(collectionIds: string[] | null = null): Promise<{
    queued: string[];
    skipped: string[];
    finished: Promise<void>;
  }> {
    const catalog = await this.loadCatalog();
    if (collectionIds) {
      const available = new Set(catalog.collections.map((collection) => collection.id));
      const missing = collectionIds.filter((id) => !available.has(id));
      if (missing.length) throw new RunError(404, "COLLECTION_NOT_FOUND", `Scheduled collections not found: ${missing.join(", ")}`);
    }
    const outcome = await this.queueSelected(
      collectionIds ?? catalog.collections.map((collection) => collection.id),
      catalog,
    );
    await Promise.all(
      outcome.skipped
        .filter((item) => item.reason === "invalid_collection")
        .map((item) => this.store.complete(item.collectionId, "error", null)),
    );
    return {
      queued: outcome.queued.map((item) => item.collectionId),
      skipped: outcome.skipped.map((item) => item.collectionId),
      finished: outcome.finished,
    };
  }

  private async queueSelected(
    ids: string[],
    catalog: Catalog,
  ): Promise<SelectedRunOutcome & { finished: Promise<void> }> {
    const descriptors = new Map(
      catalog.collections.map((collection) => [collection.id, collection]),
    );
    const skipped: SelectedRunOutcome["skipped"] = [];
    const candidates: CollectionDescriptor[] = [];

    // Reserve synchronously before the first await. This prevents two requests
    // handled by this process from accepting the same collection concurrently.
    for (const id of ids) {
      const descriptor = descriptors.get(id);
      if (!descriptor) {
        skipped.push({ collectionId: id, reason: "collection_not_found" });
      } else if (!descriptor.collection) {
        skipped.push({ collectionId: id, reason: "invalid_collection" });
      } else if (
        this.reservedCollections.has(id) ||
        this.store.get(id).status === "running"
      ) {
        skipped.push({ collectionId: id, reason: "already_running" });
      } else {
        this.reservedCollections.add(id);
        candidates.push(descriptor);
      }
    }
    // Track in the same synchronous step as the reservation, so a concurrent
    // runAllAndWait() always finds a promise to wait on for reserved collections.
    const tracked = candidates.map((descriptor) => ({
      descriptor,
      ...this.track(descriptor.id),
    }));

    const queued: SelectedRunOutcome["queued"] = [];
    const markedIds: string[] = [];
    try {
      for (const descriptor of candidates) {
        const runId = await this.store.markRunning(descriptor.id);
        markedIds.push(descriptor.id);
        queued.push({
          collectionId: descriptor.id,
          runId,
          status: "running",
          statusUrl: `/api/runs/${encodeURIComponent(runId)}`,
        });
      }
    } catch (error) {
      await Promise.all(
        markedIds.map((id) => this.store.complete(id, "error", null)),
      );
      for (const item of tracked) item.resolve();
      throw error;
    } finally {
      for (const descriptor of candidates)
        this.reservedCollections.delete(descriptor.id);
    }

    for (const item of tracked) this.enqueue(item.descriptor, item.resolve);
    const finished = Promise.all(tracked.map((item) => item.promise)).then(
      () => undefined,
    );
    return { queued, skipped, finished };
  }

  private track(id: string): { promise: Promise<void>; resolve: () => void } {
    let resolve!: () => void;
    const promise = new Promise<void>((done) => {
      resolve = done;
    });
    this.inFlight.set(id, promise);
    void promise.then(() => {
      if (this.inFlight.get(id) === promise) this.inFlight.delete(id);
    });
    return { promise, resolve };
  }

  private enqueue(descriptor: CollectionDescriptor, done: () => void): void {
    this.queue.push({ descriptor, done });
    this.drain();
  }

  private drain(): void {
    while (this.activeRuns < 10 && this.queue.length > 0) {
      const next = this.queue.shift();
      if (!next) return;
      this.activeRuns += 1;
      void this.execute(next.descriptor).finally(() => {
        this.activeRuns -= 1;
        next.done();
        this.drain();
      });
    }
  }

  private async execute(descriptor: CollectionDescriptor): Promise<void> {
    const startedAt = new Date();

    try {
      if (!descriptor.collection)
        throw new Error("Collection JSON is invalid.");
      const environment = await loadEnvironment(
        this.collectionsDir,
        descriptor.folderPath,
        descriptor.environmentFile,
      );
      const controlPlanePasswordFile =
        descriptor.folderName === "Control Plane"
          ? passwordStatePath(this.collectionsDir)
          : null;
      const originalPasswords = {
        consumer_pass: environmentValue(environment, "consumer_pass"), // gitleaks:allow
        new_password_value: environmentValue(environment, "new_password_value"),
      };
      if (controlPlanePasswordFile) {
        const passwords = await loadControlPlanePasswords(
          controlPlanePasswordFile,
          originalPasswords,
        );
        if (passwords.consumer_pass !== undefined)
          setEnvironmentValue(
            environment,
            "consumer_pass",
            passwords.consumer_pass,
          );
        if (passwords.new_password_value !== undefined)
          setEnvironmentValue(
            environment,
            "new_password_value",
            passwords.new_password_value,
          );
      }
      const currentPasswordBeforeRun = environmentValue(
        environment,
        "consumer_pass",
      );
      const initialSecrets = redactableValues(environment ?? null);
      const workingDir = path.join(this.collectionsDir, descriptor.folderPath);
      const summary = await runNewman(
        descriptor.collection,
        environment,
        workingDir,
      );
      const finalEnvironment = summary?.environment?.toJSON?.() ?? environment;
      const finalCollection =
        summary?.collection?.toJSON?.() ?? descriptor.collection;
      const finalPassword = environmentValue(finalEnvironment, "consumer_pass");
      const finalNewPassword = environmentValue(finalEnvironment, "new_password_value");
      const passwordChangeSucceeded = Boolean(
        controlPlanePasswordFile &&
          finalPassword &&
          finalNewPassword &&
          finalPassword === finalNewPassword &&
          finalPassword !== currentPasswordBeforeRun,
      );
      if (controlPlanePasswordFile && passwordChangeSucceeded && finalPassword) {
        try {
          await writeControlPlanePassword(
            controlPlanePasswordFile,
            {
              consumer_pass: finalPassword, // gitleaks:allow
              new_password_value: finalPassword,
            },
          );
          this.logger.info("Persisted successful Control Plane password change", {
            collectionId: descriptor.id,
            file: controlPlanePasswordFile,
          });
        } catch (error) {
          this.logger.error("Could not persist updated Control Plane password", {
            collectionId: descriptor.id,
            file: controlPlanePasswordFile,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }
      const secrets = [
        ...new Set([
          ...initialSecrets,
          ...redactableValues(finalEnvironment ?? null),
        ]),
      ];
      const values = environmentValues(finalEnvironment, finalCollection);
      if (
        environment &&
        finalEnvironment &&
        Array.isArray(environment.values) &&
        Array.isArray(finalEnvironment.values)
      ) {
        environment.values = finalEnvironment.values;
      }
      const result = normaliseResult(
        summary,
        descriptor.collection,
        secrets,
        startedAt,
      );
      const status =
        result.stats.requests.failed > 0 || result.stats.assertions.failed > 0
          ? "failed"
          : "passed";

      // The collection result is authoritative for collection status. Cleanup
      // is best-effort post-run work and must not erase Newman results.
      await this.store.complete(descriptor.id, status, result);

      try {
        await runPostRunManifest(
          this.collectionsDir,
          descriptor,
          values,
          secrets,
          this.logger,
        );
      } catch (error) {
        this.logger.error("Post-run cleanup failed", {
          collectionId: descriptor.id,
          runId: this.store.get(descriptor.id).runId,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    } catch (error) {
      this.logger.error("Collection execution failed", {
        collectionId: descriptor.id,
        error: error instanceof Error ? error.message : String(error),
      });
      await this.store.complete(descriptor.id, "error", null);
    }
  }
}
