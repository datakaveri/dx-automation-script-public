import assert from "node:assert/strict";
import { mkdir, writeFile } from "node:fs/promises";
import { buildRunReportHtml } from "../dist/mailer.js";
import { formatIST, formatISTDate, formatISTTime } from "../dist/formatTime.js";
assert.match(formatIST(new Date("2026-10-13T11:48:37Z")), /05:18:37 pm IST$/i);
assert.equal(formatISTDate(new Date("2026-10-13T11:48:37Z")), "13 Oct 2026");
assert.equal(formatISTTime(new Date("2026-10-13T11:48:37Z")), "05:18:37 pm");

// Run npm run build first, then node scripts/previewMail.mjs.
const startedAt = new Date("2026-10-13T04:30:00Z");
const completedAt = new Date("2026-10-13T04:32:15Z");
await mkdir("previews", { recursive: true });
for (const failed of [false, true]) {
  const collections = ["Control Plane", "Data Plane", "File Connect"].map((folderName, index) => ({
    id: folderName.toLowerCase().replaceAll(" ", "-"), folderName,
    runId: `preview-run-${index}`, status: failed && index === 2 ? "failed" : "passed",
    apiCount: 40, tests: { passed: failed && index === 2 ? 38 : 40, failed: failed && index === 2 ? 2 : 0, total: 40 },
    assertions: { passed: failed && index === 2 ? 78 : 80, failed: failed && index === 2 ? 2 : 0, total: 80 },
  }));
  const report = { startedAt, completedAt, data: {
    kpis: { totalCollections: 3, passedCollections: failed ? 2 : 3, failedCollections: failed ? 1 : 0, totalApis: 120, failedAssertions: failed ? 2 : 0 }, collections,
  } };
  const html = buildRunReportHtml(report, "http://localhost:4000", "https://iudxqa-dev.slicearrow.com");
  assert.ok(html.includes("Hi Team,"));
  assert.ok(html.includes("Daily API Health Report"));
  assert.ok(html.includes("Execution Details"));
  assert.ok(html.includes("Execution Summary"));
  assert.ok(html.includes("Collection Results"));
  assert.ok(html.includes("QA Dashboard"));
  assert.ok(html.includes("Open QA Dashboard"));
  assert.ok(html.includes("IUDX QA Automation"));
  assert.ok(html.includes("13 Oct 2026"));
  assert.ok(html.includes("10:00:00 am IST"));
  assert.ok(html.includes("10:02:15 am IST"));
  assert.ok(html.includes("2m 15s"));
  assert.ok(html.includes("IST"));
  assert.ok(!html.includes("GMT"));
  assert.ok(html.includes(`Total Collections:</strong> 3`));
  assert.ok(html.includes(`Collections Passed:</strong> ${failed ? 2 : 3}`));
  assert.ok(html.includes(`Collections Failed:</strong> ${failed ? 1 : 0}`));
  assert.ok(html.includes("/api/collections/file-connect/history/preview-run-2/report"));
  assert.ok(!html.includes("<script"));
  const file = `previews/mail-${failed ? "failed" : "passed"}.html`;
  await writeFile(file, html, "utf8");
  console.log(`Created ${file}`);
}
const escaped = buildRunReportHtml({ startedAt, completedAt, data: {
  kpis: { totalCollections: 1, passedCollections: 0, failedCollections: 1, totalApis: 0, failedAssertions: 0 },
  collections: [{ id: "example", folderName: '<img src=x onerror="alert(1)">', status: "error", apiCount: 0, tests: { passed: 0, failed: 0, total: 0 }, assertions: { failed: 0 } }],
} }, "http://localhost:4000", "https://example.com");
assert.ok(escaped.includes("&lt;img"));
assert.ok(!escaped.includes("<img"));
assert.ok(!escaped.includes(">View report</a>"));
console.log("Verified report states, timing, links, escaping, and unavailable reports.");
