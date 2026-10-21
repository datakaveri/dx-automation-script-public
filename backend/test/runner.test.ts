import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { after, before, describe, it } from "node:test";
import { generateNewmanHtmlReport } from "../src/reportGenerator";
import { normaliseResult, runNewman } from "../src/runner";
import type { CollectionDescriptor, CollectionState, RunResult } from "../src/types";

// A real Newman run against a local server, so the normalised result is checked
// against what Newman actually reported.
let server: Server;
let baseUrl = "";
let result: RunResult;
let report = "";

// Fake credentials generated per run, so the checks below can prove they never reach results.
const TOKEN = `fixture-${randomUUID()}`;
const PASSWORD = `fixture-${randomUUID()}`;

const test = (lines: string[]) => [{ listen: "test", script: { type: "text/javascript", exec: lines } }];
const request = (name: string, path: string, lines: string[]) => ({
  name,
  request: { method: "GET", url: `{{base_url}}${path}` },
  event: test(lines),
});

function collection(closedPortUrl: string): Record<string, unknown> {
  return {
    info: { name: "runner fixture", schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json" },
    variable: [{ key: "base_url", value: baseUrl }, { key: "token", value: TOKEN }, { key: "password", value: PASSWORD }],
    auth: { type: "bearer", bearer: [{ key: "token", value: "{{token}}", type: "string" }] },
    item: [
      {
        name: "Folder A",
        item: [
          // Name says 404 and the response is 404, but the JSON check really fails.
          request("Missing Route 404", "/plain-404", [
            'pm.test("Status code is 404", () => pm.response.to.have.status(404));',
            'pm.test("Content-Type is JSON", () => pm.expect(pm.response.headers.get("Content-Type")).to.include("application/json"));',
            'postman.setNextRequest("Poll Until Ready 200");',
          ]),
          // Skipped by setNextRequest above.
          request("Skipped Request 200", "/ok", ['pm.test("Status code is 200", () => pm.response.to.have.status(200));']),
        ],
      },
      {
        name: "Folder B",
        item: [
          // Runs three times; assertions only on the last run.
          request("Poll Until Ready 200", "/ok", [
            'const n = Number(pm.variables.get("polls") || 0) + 1;',
            'pm.variables.set("polls", String(n));',
            'if (n < 3) { postman.setNextRequest(pm.info.requestName); }',
            'else { pm.test("Status code is 200", () => pm.response.to.have.status(200)); }',
          ]),
          request("Ready 200", "/ok", ['pm.test("Status code is 200", () => pm.response.to.have.status(200));']),
          {
            name: "Login 200",
            request: {
              method: "POST",
              url: "{{base_url}}/login",
              body: { mode: "urlencoded", urlencoded: [{ key: "username", value: "qa-user" }, { key: "password", value: "{{password}}" }] },
            },
            event: test(['pm.test("Status code is 200", () => pm.response.to.have.status(200));']),
          },
          {
            name: "Create Item 201",
            request: {
              method: "POST",
              url: "{{base_url}}/items",
              header: [{ key: "Content-Type", value: "application/json" }],
              body: { mode: "raw", raw: '{"name": "widget", "count": 2}' },
            },
            event: test(['pm.test("Status code is 201", () => pm.response.to.have.status(201));']),
          },
          {
            name: "Unreachable 200",
            request: { method: "GET", url: closedPortUrl },
            event: test(['pm.test("Status code is 200", () => pm.response.to.have.status(200));']),
          },
        ],
      },
    ],
  };
}

before(async () => {
  server = createServer((req, res) => {
    if (req.url === "/plain-404") {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }).end("404 page not found");
    } else if (req.url === "/items") {
      res.writeHead(201, { "Content-Type": "application/json" }).end('{"id":1}');
    } else {
      res.writeHead(200, { "Content-Type": "application/json" }).end('{"ok":true}');
    }
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  baseUrl = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;

  // A port that was open and is now closed gives a connection error (no response).
  const probe = createServer();
  await new Promise<void>((resolve) => probe.listen(0, "127.0.0.1", resolve));
  const closedPortUrl = `http://127.0.0.1:${(probe.address() as AddressInfo).port}/ok`;
  await new Promise<void>((resolve) => probe.close(() => resolve()));

  const fixture = collection(closedPortUrl);
  const summary = await runNewman(fixture, undefined, process.cwd());
  result = normaliseResult(summary, fixture, [PASSWORD], new Date());

  const descriptor = { id: "fixture", folderName: "Fixture", name: "runner fixture", fileName: "fixture.json", environment: null } as unknown as CollectionDescriptor;
  const state = { status: "failed", lastRunAt: result.completedAt, durationMs: result.durationMs, stats: result.stats, result } as CollectionState;
  report = generateNewmanHtmlReport(descriptor, state, result, "/");
});

after(() => {
  server.close();
});

const execution = (name: string) => {
  const found = result.executions.find((e) => e.name === name);
  assert.ok(found, `missing execution ${name}`);
  return found;
};

describe("normaliseResult", () => {
  it("keeps Newman's assertion failures even when the status matches the request name", () => {
    const missing = execution("Missing Route 404");
    assert.equal(missing.statusCode, 404);
    assert.deepEqual(
      missing.assertions.map((a) => [a.name, a.passed]),
      [["Status code is 404", true], ["Content-Type is JSON", false]],
    );
    assert.equal(missing.failed, true);
  });

  it("assigns each execution the folder of its own request, despite skipped and repeated requests", () => {
    assert.equal(execution("Missing Route 404").folder, "Folder A");
    assert.equal(execution("Poll Until Ready 200").folder, "Folder B");
    assert.equal(execution("Ready 200").folder, "Folder B");
    assert.equal(execution("Unreachable 200").folder, "Folder B");
  });

  it("counts a repeated request once, using its final run", () => {
    const polls = result.executions.filter((e) => e.name === "Poll Until Ready 200");
    assert.equal(polls.length, 1);
    assert.deepEqual(polls[0].assertions.map((a) => a.name), ["Status code is 200"]);
  });

  it("does not report requests that never ran", () => {
    assert.equal(result.executions.some((e) => e.name === "Skipped Request 200"), false);
  });

  it("reports a request without a response as failed with no status code", () => {
    const unreachable = execution("Unreachable 200");
    assert.equal(unreachable.statusCode, null);
    assert.ok(unreachable.requestError);
    assert.equal(unreachable.failed, true);
  });

  it("derives request and test totals from executed requests", () => {
    assert.equal(result.executions.length, 6);
    assert.deepEqual(result.stats.requests, { total: 6, failed: 2 });
    assert.deepEqual(result.stats.tests, { total: 6, failed: 2 });
    assert.deepEqual(result.stats.assertions, { total: 7, failed: 2 });
  });

  it("lists the collection requests that never ran", () => {
    assert.deepEqual(
      result.skippedRequests?.map((r) => [r.folder, r.name, r.method]),
      [["Folder A", "Skipped Request 200", "GET"]],
    );
  });

  it("records the headers each request really sent, with credentials hidden", () => {
    const headers = execution("Ready 200").requestHeaders ?? [];
    const value = (key: string) => headers.find((h) => h.key.toLowerCase() === key)?.value;
    assert.equal(value("host"), new URL(baseUrl).host);
    assert.equal(value("authorization"), "Bearer [REDACTED]");
    assert.ok(!JSON.stringify(result).includes(TOKEN), "token leaked into the result");
  });

  it("records the body each request really sent, with secrets hidden", () => {
    assert.equal(execution("Create Item 201").requestBody, '{"name": "widget", "count": 2}');
    assert.equal(execution("Login 200").requestBody, "username=qa-user&password=[REDACTED]");
    assert.equal(execution("Ready 200").requestBody ?? null, null);
    assert.ok(!JSON.stringify(result).includes(PASSWORD), "password leaked into the result");
  });
});

describe("generateNewmanHtmlReport", () => {
  it("shows the real request host and body, not placeholder text", () => {
    assert.ok(report.includes(new URL(baseUrl).host));
    assert.ok(report.includes("widget"));
    assert.ok(!report.includes("keyValues"));
    assert.ok(!report.includes("[REDACTED_JWT_TOKEN]"));
  });

  it("counts and lists the requests that never ran as skipped", () => {
    assert.match(report, /Skipped Tests <span class="badge badge-light">1<\/span>/);
    assert.ok(report.includes("Skipped Request 200"));
    assert.ok(!report.includes("There are no skipped tests"));
  });
});
