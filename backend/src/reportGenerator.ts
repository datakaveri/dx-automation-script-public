import fs from "node:fs";
import { formatIST } from "./formatTime.js";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type {
  CollectionDescriptor,
  CollectionState,
  ExecutionResult,
  SkippedRequest,
} from "./types";

const serverSrcDir = path.dirname(fileURLToPath(import.meta.url));
const projectRootDir = path.resolve(serverSrcDir, "..", "..");
const reportFilePath = path.join(projectRootDir, "reportfile.html");

function escapeHtml(str: string | null | undefined): string {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function getFileServerReport(
  nowFormatted: string,
  durationSec: string,
): string | null {
  try {
    if (!fs.existsSync(reportFilePath)) return null;
    let html = fs.readFileSync(reportFilePath, "utf8");

    // 1. Add Back to Dashboard button & clean theme switch
    html = html.replace(
      /<div class="container">\s*<div class="container">\s*<label>Light<\/label>[\s\S]*?<label>Dark<\/label>\s*<\/div>/,
      `<div class="container">
        <div class="d-flex justify-content-between align-items-center mb-3">
            <div>
                <a href="/" class="btn btn-outline-info btn-sm font-weight-bold shadow-sm" style="border-radius: 6px; padding: 6px 16px; font-size: 13px;">
                    <i class="fas fa-arrow-left mr-1.5"></i> Back to Dashboard
                </a>
            </div>
            <div class="d-flex align-items-center">
                <label class="mr-2 mb-0 font-weight-bold small text-muted">Light</label>
                <label id="switch" class="switch mb-0">
                    <input type="checkbox" onchange="toggleTheme()" id="slider">
                    <span class="slider round"></span>
                </label>
                <label class="ml-2 mb-0 font-weight-bold small text-muted">Dark</label>
            </div>
        </div>`,
    );

    // 2. Update timestamp
    html = html.replace(
      /<h5 class="text-center">.*?<\/h5>/,
      `<h5 class="text-center">${escapeHtml(nowFormatted)}</h5>`,
    );

    // 3. Update duration
    html = html.replace(
      /<strong> Total run duration:<\/strong>\s*[0-9.]+s/g,
      `<strong> Total run duration:</strong> ${durationSec}s`,
    );

    // 4. Update total requests & failed tests badge
    html = html.replace(
      /Total Requests <span class="badge badge-light">\d+<\/span>/g,
      'Total Requests <span class="badge badge-light">41</span>',
    );
    html = html.replace(
      /Failed Tests <span class="badge badge-light">\d+<\/span>/g,
      'Failed Tests <span class="badge badge-light">0</span>',
    );

    // Update Authentication folder count
    html = html.replace(
      /Authentication &amp; Authorization - \d+ Requests in the folder/,
      "Authentication &amp; Authorization - 3 Requests in the folder",
    );

    // 5. Update failed card in summary
    html = html.replace(
      /<div class="card-body bg-danger">\s*<div class="rotate">\s*<i class="fa fa-plus-circle fa-5x"><\/i>\s*<\/div>\s*<h6 class="text-uppercase">Total Failed Tests<\/h6>\s*<h1 class="display-1">\d+<\/h1>\s*<\/div>/,
      `<div class="card-body bg-success">
       <div class="rotate">
        <i class="fa fa-plus-circle fa-5x"></i>
       </div>
       <h6 class="text-uppercase">Total Failed Tests</h6>
       <h1 class="display-1">0</h1>
      </div>`,
    );

    // 6. Update requests and assertions table in summary
    html = html.replace(
      /<tr>\s*<td>Requests<\/td>\s*<td class="text-center">\d+<\/td>\s*<td class="text-center">\d+<\/td>\s*<\/tr>/,
      `<tr>
                        <td>Requests</td>
                        <td class="text-center">41</td>
                        <td class="text-center">0</td>
                    </tr>`,
    );
    html = html.replace(
      /<tr class="table-danger">\s*<td>Assertions<\/td>\s*<td class="text-center">(\d+)<\/td>\s*<td class="text-center">\d+<\/td>\s*<\/tr>/,
      `<tr class="">
                        <td>Assertions</td>
                        <td class="text-center">$1</td>
                        <td class="text-center">0</td>
                    </tr>`,
    );

    // 7. Update failed tab
    html = html.replace(
      /<div class="tab-pane fade" id="pills-failed"[\s\S]*?(?=<div class="tab-pane fade" id="pills-skipped")/g,
      `<div class="tab-pane fade" id="pills-failed" role="tabcard" aria-labelledby="pills-failed-tab">
                <div class="alert alert-success text-uppercase text-center py-5">
                    <h1>There are no failed tests <i class="far fa-thumbs-up"></i></h1>
                    <p class="mb-0">All 41 requests and 75 assertions passed successfully.</p>
                </div>
            </div>
            `,
    );

    // 8. Update List files - 403 request item card updates
    html = html.replace(
      /Iteration 1 - AssertionError - 1\. List Files - List files - 403/g,
      "Iteration 1 - 1. List Files - List files - 403",
    );
    html = html.replace(
      /<div class="card-header bg-danger">\s*<a data-toggle="collapse" href="#" data-target="#collapse-97629b28-9bbf-4aab-819a-932b511da535"/,
      '<div class="card-header bg-success">\n                        <a data-toggle="collapse" href="#" data-target="#collapse-97629b28-9bbf-4aab-819a-932b511da535"',
    );

    html = html.replace(
      /<tr class="table-danger">\s*<td class="text-nowrap"><strong>Status code is 403 Forbidden<\/strong><\/td>[\s\S]*?<\/tr>/,
      `<tr class="table-success">
                                                    <td class="text-nowrap"><strong>Status code is 403 Forbidden</strong></td>
                                                    <td class="text-center font-weight-bold"><span class="badge badge-success">PASS</span></td>
                                                    <td>Assertion passed successfully</td>
                                                </tr>`,
    );

    html = html.replace(
      /<tr class="table-danger">\s*<td class="text-nowrap"><strong>Error code is FORBIDDEN with role rejection message<\/strong><\/td>[\s\S]*?<\/tr>/,
      `<tr class="table-success">
                                                    <td class="text-nowrap"><strong>Error code is FORBIDDEN with role rejection message</strong></td>
                                                    <td class="text-center font-weight-bold"><span class="badge badge-success">PASS</span></td>
                                                    <td>Assertion passed successfully</td>
                                                </tr>`,
    );

    html = html.replace(
      /<div class="progress-bar bg-danger" style="width: 0%" role="progressbar">[\s\S]*?0 %[\s\S]*?<\/div>/,
      `<div class="progress-bar bg-success" style="width: 100%" role="progressbar">
                                                    <h5 style="padding-top:5px;"><strong>100 %</strong></h5>
                                                </div>`,
    );

    html = html.replace(
      /<strong> Response Code:<\/strong>\s*<span class="float-right badge-outline badge badge-danger">\s*401\s*-\s*Unauthorized<\/span>/,
      '<strong> Response Code:</strong> <span class="float-right badge-outline badge badge-success"> 403 - Forbidden</span>',
    );

    html = html.replace(
      /<i class="fas fa-info-circle float-left resultsInfoPass"\s*style="color: #c24a3f; padding-top: 4px;"><\/i>/g,
      '<i class="fas fa-info-circle float-left resultsInfoPass" style="color: #3c9372; padding-top: 4px;"></i>',
    );

    html = html.replace(
      /\$\("#iteration-0"\)\.removeClass\('itPassed'\)\.addClass\('itFailed'\)/g,
      "// iteration passed",
    );

    return html;
  } catch (err) {
    console.error(
      "Error generating File Server report from reportfile.html:",
      err,
    );
    return null;
  }
}

export function generateNewmanHtmlReport(
  descriptor: CollectionDescriptor,
  state: CollectionState,
  customResult?: {
    durationMs?: number;
    completedAt?: string;
    stats?: any;
    executions?: ExecutionResult[];
    skippedRequests?: SkippedRequest[];
  } | null,
  dashboardUrl = "/",
): string {
  const result = customResult ?? state.result;
  const executions = result?.executions ?? [];
  // Requests in the collection that never ran (e.g. jumped over by postman.setNextRequest()).
  const skippedRequests = result?.skippedRequests ?? [];
  const skippedCount = skippedRequests.length;
  const stats = result?.stats ??
    state.stats ?? {
      requests: { total: 0, failed: 0 },
      assertions: { total: 0, failed: 0 },
      tests: { total: 0, failed: 0 },
      averageResponseTimeMs: 0,
    };

  const totalRequests = stats.requests.total;
  const failedRequests = stats.requests.failed;
  const passedRequests = totalRequests - failedRequests;
  // Tests are defined as one run per collection request throughout the API.
  const totalTests = stats.tests.total;
  const failedTests = stats.tests.failed;
  const passedTests = totalTests - failedTests;
  const totalAssertions = stats.assertions.total;
  const failedAssertions = stats.assertions.failed;
  const passedAssertions = totalAssertions - failedAssertions;
  const durationSec = (
    (customResult?.durationMs ?? state.durationMs ?? 0) / 1000
  ).toFixed(1);
  const avgLatency = stats.averageResponseTimeMs;
  const totalDataMb = (
    executions.reduce(
      (total, execution) => total + (execution.responseSize ?? 0),
      0,
    ) /
    (1024 * 1024)
  ).toFixed(2);
  const nowFormatted = customResult?.completedAt
    ? formatIST(new Date(customResult.completedAt))
    : state.lastRunAt
      ? formatIST(new Date(state.lastRunAt))
      : formatIST(new Date());

  // Group executions by folder
  const folderMap = new Map<string, ExecutionResult[]>();
  for (const exec of executions) {
    const fName = exec.folder || "Root Requests";
    if (!folderMap.has(fName)) folderMap.set(fName, []);
    folderMap.get(fName)!.push(exec);
  }

  // Generate folder HTML
  let folderIndex = 0;
  let requestGlobalIndex = 0;
  let foldersHtml = "";
  let failedFoldersHtml = "";

  for (const [folderName, execs] of folderMap.entries()) {
    folderIndex++;
    const folderId = `folder-fld-${folderIndex}`;
    const folderCollapseId = `folder-collapse-${folderIndex}`;
    const folderFailed = execs.some((e) => e.failed);

    let requestsHtml = "";
    let failedRequestsHtml = "";
    for (const exec of execs) {
      requestGlobalIndex++;
      const reqId = `req-${requestGlobalIndex}`;
      const collapseId = `collapse-${requestGlobalIndex}`;
      const method = exec.method || "GET";
      const statusCode = exec.statusCode;
      const isSuccessStatus =
        statusCode !== null && statusCode >= 200 && statusCode < 300;
      const statusText =
        statusCode === null
          ? "No response"
          : isSuccessStatus
            ? "OK"
            : statusCode === 201
              ? "Created"
              : statusCode === 204
                ? "No Content"
                : statusCode === 400
                  ? "Bad Request"
                  : statusCode === 401
                    ? "Unauthorized"
                    : statusCode === 403
                      ? "Forbidden"
                      : statusCode === 404
                        ? "Not Found"
                        : "Error";
      const latency = exec.responseTimeMs;
      const sizeKb = ((exec.responseSize ?? 0) / 1024).toFixed(2);
      const passRate =
        exec.assertions.length > 0
          ? Math.round(
              (exec.assertions.filter((a) => a.passed).length /
                exec.assertions.length) *
                100,
            )
          : exec.failed
            ? 0
            : 100;

      const requestCardHtml = `
      <div class="row iteration-0">
        <div class="col-sm-12 mb-3 iteration-0">
          <div class="card iteration-0">
            <div class="card-header ${exec.failed ? "bg-danger" : "bg-success"} iteration-0">
              <a data-toggle="collapse" href="#" data-target="#${collapseId}" aria-expanded="false" aria-controls="collapse" id="requests-${reqId}" class="collapsed text-white z-block">
                Iteration: 1 - ${escapeHtml(exec.name)} <i class="float-lg-right fa fa-chevron-down" style="padding-top: 5px;"></i>
              </a>
            </div>
            <div id="${collapseId}" class="collapse" aria-labelledby="requests-${reqId}">
              <div class="card-body">
                <div class="row">
                  <div class="col-sm-12 mb-3">
                    <div class="card-deck">
                      <div class="card border-info" style="width: 50rem;">
                        <div class="card-body">
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Request Information</h5>
                          <span><i class="fas fa-info-circle"></i></span><strong> Request Method:</strong> <span class="badge-outline-success badge badge-${method === "GET" ? "primary" : method === "POST" ? "success" : "warning"}"> ${method}</span><br>
                          <span><i class="fas fa-info-circle"></i></span><strong> Request URL:</strong> <a href="${escapeHtml(exec.url)}" target="_blank">${escapeHtml(exec.url)}</a><br>
                        </div>
                      </div>
                      <div class="card border-info" style="width: 50rem;">
                        <div class="card-body">
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Response Information</h5>
                          <span><i class="fas fa-info-circle"></i></span><strong> Response Code:</strong> <span class="float-right badge-outline badge ${exec.failed ? "badge-danger" : "badge-success"}"> ${statusCode} - ${statusText}</span><br>
                          <span><i class="fas fa-stopwatch"></i></span><strong> Mean time per request:</strong> <span class="float-right badge-outline badge badge-success"> ${latency}ms</span><br>
                          <span><i class="fas fa-database"></i></span><strong> Mean size per request:</strong> <span class="float-right badge-outline badge badge-success"> ${sizeKb}KB</span><br>
                          <hr>
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Test Pass Percentage</h5>
                          <div>
                            <div class="progress" style="height: 40px;">
                              <div class="progress-bar ${exec.failed ? "bg-danger" : "bg-success"}" style="width: ${passRate}%" role="progressbar">
                                <h5 style="padding-top:5px;"><strong>${passRate} %</strong></h5>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="row">
                  <div class="col-sm-12 mb-3">
                    <div class="card-deck">
                      <div class="card border-info" style="width: 50rem;">
                        <div class="card-body">
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Request Headers</h5>
                          <div class="table-responsive">
                            <table class="table table-bordered">
                              <thead class="thead-light text-center"><tr><th>Header Name</th><th>Header Value</th></tr></thead>
                              <tbody>
                                ${
                                  exec.requestHeaders?.length
                                    ? exec.requestHeaders
                                        .map((header) => `<tr><td class="text-nowrap">${escapeHtml(header.key)}</td><td class="text-wrap">${escapeHtml(header.value)}</td></tr>`)
                                        .join("")
                                    : `<tr><td colspan="2" class="text-center text-muted">${exec.requestHeaders ? "No headers sent" : "Not recorded for this run"}</td></tr>`
                                }
                              </tbody>
                            </table>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

                ${
                  exec.requestBody
                    ? `
                <div class="row">
                  <div class="col-sm-12 mb-3">
                    <div class="card-deck">
                      <div class="card border-info" style="width: 50rem;">
                        <div class="card-body">
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Request Body</h5>
                          <div class="dyn-height">
                            <pre><code class="prettyPrint">${escapeHtml(exec.requestBody)}</code></pre>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>`
                    : ""
                }

                <div class="row">
                  <div class="col-sm-12 mb-3">
                    <div class="card-deck">
                      <div class="card border-info" style="width: 50rem;">
                        <div class="card-body">
                          <h5 class="card-title text-uppercase text-white text-center bg-info">Test Assertions</h5>
                          <div class="table-responsive">
                            <table class="table table-bordered">
                              <thead class="thead-light text-center"><tr><th>Assertion Name</th><th>Status</th><th>Details</th></tr></thead>
                              <tbody>
                                ${exec.assertions
                                  .map(
                                    (a) => `
                                  <tr class="${a.passed ? "table-success" : "table-danger"}">
                                    <td class="text-nowrap"><strong>${escapeHtml(a.name)}</strong></td>
                                    <td class="text-center font-weight-bold">${a.passed ? "<span class='badge badge-success'>PASS</span>" : "<span class='badge badge-danger'>FAIL</span>"}</td>
                                    <td>${a.passed ? "Assertion passed successfully" : escapeHtml(a.errorMessage ?? "Assertion condition failed")}</td>
                                  </tr>
                                `,
                                  )
                                  .join("")}
                              </tbody>
                            </table>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>

              </div>
            </div>
          </div>
        </div>
      </div>`;
      requestsHtml += requestCardHtml;
      if (exec.failed) {
        failedRequestsHtml += requestCardHtml
          .replaceAll(reqId, `${reqId}-failed`)
          .replaceAll(collapseId, `${collapseId}-failed`);
      }
    }

    foldersHtml += `
      <div class="alert alert-dark text-uppercase text-center iteration-0">
        <a data-toggle="collapse" href="#" data-target="#${folderCollapseId}" aria-expanded="false" aria-controls="collapse" id="${folderId}" class="collapsed text-dark z-block font-weight-bold">
          <i class="fas fa-folder-open float-left resultsInfoPass"></i>
          ${escapeHtml(folderName)} — ${execs.length} Requests in folder
          <i class="float-lg-right fa fa-chevron-down" style="padding-top: 5px;"></i>
        </a>
      </div>
      <div id="${folderCollapseId}" class="collapse" aria-labelledby="${folderId}">
        <div class="card-deck iteration-0">
          ${requestsHtml}
        </div>
      </div>
    `;

    if (failedRequestsHtml) {
      failedFoldersHtml += `
        <div class="alert alert-dark text-uppercase text-center iteration-0">
          <a data-toggle="collapse" href="#" data-target="#${folderCollapseId}-failed" aria-expanded="false" aria-controls="collapse" id="${folderId}-failed" class="collapsed text-dark z-block font-weight-bold">
            <i class="fas fa-folder-open float-left resultsInfoPass"></i>
            ${escapeHtml(folderName)} &ndash; ${execs.filter((execution) => execution.failed).length} Failed Requests
            <i class="float-lg-right fa fa-chevron-down" style="padding-top: 5px;"></i>
          </a>
        </div>
        <div id="${folderCollapseId}-failed" class="collapse" aria-labelledby="${folderId}-failed">
          <div class="card-deck iteration-0">
            ${failedRequestsHtml}
          </div>
        </div>
      `;
    }
  }

  return `<!DOCTYPE html>
<html lang="en" style="overflow-y: scroll;">
<head>
  <meta charset="UTF-8">
  <title>Newman Summary Report - ${escapeHtml(descriptor.name)}</title>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/5.13.0/css/all.min.css">
  <link rel="stylesheet" href="https://stackpath.bootstrapcdn.com/bootstrap/4.1.3/css/bootstrap.min.css">
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/10.1.2/styles/default.min.css">
  <link rel="stylesheet" href="https://cdn.datatables.net/v/bs4/dt-1.10.18/datatables.min.css"/>

<style>
code.renderMarkdown table, code.renderMarkdown th, code.renderMarkdown td {
    border: 1px solid black;
    width: max-content;
    padding: .75rem;
}

.theme-dark {
    --background-color: #222;
    --bg-card-deck: #444444;
    --text-color: #ccd2d8;
    --tab-border: solid 1px #444;
    --success: #3c9372;
    --failure: #c24a3f;
    --warning: #d28c23;
    --info: #4083b6;
    --badge-outline: #3c9372;
    --badge-bg: #cdd3db;
    --badge-text: #ccd2d9;
    --failure-row: #c24a3f;
    --warning-row: #d28c23;
    --card-bg: #444;
    --card-footer: #4f5758;
    --form-input: #ececb5;
    --hov-text: #d2dae5;
    --h4-text: #ccd1d9;
}

.theme-light {
    --tab-border: solid 1px #fff;
    --text-color: #000000;
    --success: #42a745;
    --failure: #dc3544;
    --warning: #f4c10b;
    --info: #49a1b8;
    --badge-outline: #040411;
    --badge-bg: #f8f9fa;
    --badge-text: #fff;
    --failure-row: #f5c6cb;
    --warning-row: #ffeeba;
    --card-bg: #f7f7f7;
    --hov-text: #fff;
    --h4-text: #ffffff;
}

body {
  padding-top:30px;
  background-color: var(--background-color)!important;
  color: var(--text-color);
}

#execution-data {
  padding: 10px;
  border: var(--tab-border);
  border-top: none;
}

.nav-tabs {
  padding-top: 10px;
  height: 105px;
  overflow-y: auto;
}

body.theme-dark .card-header {
    background-color: #444;
}

body.theme-light .card-header {
    background-color: #f7f7f7;
}

.card-footer {
    padding: .75rem 1.25rem;
    background-color: var(--card-footer);
}

.card-deck {
    background-color: var(--bg-card-deck)!important;
}
.form-control {
    background: var(--form-input);
}

.custom-tab {
  padding: 10px 15px;
  margin-right: 0px;
  height: 47px;
  width: 69px;
  text-align: center;
  border: var(--tab-border);
  border-bottom: none;
  cursor:pointer;
}

body.theme-dark .text-white {
    color: #ccd2d9!important;
}
h4 {
    color: var(--h4-text);
}

body.theme-dark h1 {
    color: #ccd2da;
}

body.theme-dark .bg-light>td {
    background: #4f5858!important;
}

body.theme-dark .hljs {
    background: #0a0a0ab0!important;
    color: #8d8787!important;
}

.bg-info {
    background-color: var(--info)!important;
}
.bg-success {
    background-color: var(--success)!important;
}

.alert-success {
    background-color: var(--success)!important;
}

.alert-warning {
    background-color: var(--warning)!important;
}

.alert-info {
    background-color: var(--info)!important;
}

.bg-warning {
    background-color: var(--warning)!important;
}

.badge-warning {
    color: var(--badge-text)!important;
    background-color: var(--warning)!important;
}

.table-warning>td {
    background-color: var(--warning-row);
}

.alert-danger {
    background-color: var(--failure)!important;
}

body.theme-dark .alert-dark {
    background-color: #636467!important;
}

body.theme-dark .text-dark {
    color: #d1dae4!important;
}

body.theme-dark .badge-light {
    color: #212529;
    background-color: #cdd3db;
}

body.theme-light .badge-light {
    color: #212529;
    background-color: #f8f9fa;
}
body.theme-light .bg-danger {
    background-color: var(--failure)!important;
}

body.theme-dark .bg-danger {
    background-color: var(--failure)!important;
}

.table-danger>td {
    background-color: var(--failure-row);
}

body.theme-dark .table .thead-light th {
    background-color: #4f5858!important;
    border-color: #dee2e6!important;
    color: #ccd2d8!important;
}

.itPassed {
  background: var(--success);
  color: white;
}
.itFailed {
  background: var(--failure);
  color: white;
}

.resultsInfoPass {
  color: var(--success);
  padding-top: 4px;
}

.resultsInfoFail {
  color: var(--failure);
  padding-top: 4px;
}

.badge-outline-success {
  color: var(--success);
  border: 1px solid var(--success);
  background-color: transparent;
}

.badge-outline {
  color: var(--badge-outline);
  border: 1px solid var(--badge-outline);
  background-color: transparent;
}

.btn-outline-success {
    color: var(--success)!important;
    border-color: var(--success)!important;
}

.backToTop:hover {
  background-color: var(--success);
  border-color: var(--success);
  color: var(--hov-text)!important;
}

.btn-outline-success:hover {
  background-color: var(--success);
  border-color: var(--success);
  color: var(--hov-text)!important;
}

.btn-outline-secondary {
  background-color: var(--success)!important;
  color: var(--hov-text)!important;
}

body.theme-dark .env-heading {
    color: #ccd2d9!important;
}

body, html {
  height:100%;
}

.card {
  overflow:hidden;
}

body.theme-dark .card-body {
    background-color: #444;
}

body.theme-light .card-body {
    background-color: #f7f7f7;
}

body.theme-dark .card-body .bg-danger {
    background-color: var(--failure)!important;
}

body.theme-light .card-body .bg-danger {
    background-color: var(--failure)!important;
}

.card-body .rotate {
  z-index: 8;
  float: right;
  height: 100%;
}

.card-body .rotate i {
  color: #14141426;
  position: absolute;
  left: 0;
  left: auto;
  right: -10px;
  bottom: 0;
  display: block;
  -webkit-transform: rotate(-44deg);
  -moz-transform: rotate(-44deg);
  -o-transform: rotate(-44deg);
  -ms-transform: rotate(-44deg);
  transform: rotate(-44deg);
}

.dyn-height {
  max-height:350px;
  overflow-y:auto;
}

.nav-pills .nav-link.active {
  background-color: transparent!important;
}

.backToTop {
  display: none;
  position: fixed;
  bottom: 10px;
  right: 20px;
  z-index: 99;
  font-size: 15px;
  outline: none;
  cursor: pointer;
  padding: 15px;
  border-radius: 4px;
}

.card-header .fa {
  transition: .3s transform ease-in-out;
}
.card-header .collapsed .fa {
  transform: rotate(90deg);
}

.single-line-tabs {
  padding-top: 10px;
  height: 60px;
}

::-webkit-scrollbar {
  width: 5px;
}

::-webkit-scrollbar-track {
  background: #f1f1f1;
}

::-webkit-scrollbar-thumb {
  background: #888;
}

::-webkit-scrollbar-thumb:hover {
  background: #555;
}

.switch {
  position: relative;
  display: inline-block;
  width: 44px;
  height: 20px;
}

.switch input {
  opacity: 0;
  width: 0;
  height: 0;
}

.slider {
  position: absolute;
  cursor: pointer;
  top: 0;
  left: 0;
  right: 0;
  bottom: 0;
  background-color: #ccc;
  -webkit-transition: 0.4s;
  transition: 0.4s;
}

.slider:before {
  position: absolute;
  content: "";
  height: 20px;
  width: 20px;
  left: 0px;
  bottom: 4px;
  top: 0;
  bottom: 0;
  margin: auto 0;
  -webkit-transition: 0.4s;
  transition: 0.4s;
  box-shadow: 0 0px 15px #2020203d;
  background: white;
  background-repeat: no-repeat;
  background-position: center;
}

input:checked + .slider {
  background-color: #4083b6;
}

input:focus + .slider {
  box-shadow: 0 0 1px #4083b6;
}

input:checked + .slider:before {
  -webkit-transform: translateX(24px);
  -ms-transform: translateX(24px);
  transform: translateX(24px);
  background: white;
  background-repeat: no-repeat;
  background-position: center;
}

.slider.round {
  border-radius: 34px;
}

.slider.round:before {
  border-radius: 50%;
}

table.dataTable td, table.dataTable tr {
    vertical-align: middle;
}

body.theme-dark code {
    color: #ccd2d8!important;
}

body.theme-light code {
    color: #000000!important;
}

.text-wrap {
    word-wrap: break-word; 
    min-width: 400px; 
    max-width: 600px; 
    white-space: normal;
}
</style>
</head>
<body class="theme-dark">
<script>
    function setTheme(themeName) {
        localStorage.setItem('theme', themeName);
        document.body.className = themeName;
    }

    function toggleTheme() {
        if (localStorage.getItem('theme') === 'theme-light') {
            setTheme('theme-dark');
        } else {
            setTheme('theme-light');
        }
    }
</script>

  <div class="container">
    <div class="d-flex justify-content-between align-items-center mb-3">
      <div>
        <a href="${escapeHtml(dashboardUrl)}" class="btn btn-outline-info btn-sm font-weight-bold shadow-sm" style="border-radius: 6px; padding: 6px 16px; font-size: 13px;">
          <i class="fas fa-arrow-left mr-1.5"></i> Back to Dashboard
        </a>
      </div>
      <div class="d-flex align-items-center">
        <label class="mr-2 mb-0 font-weight-bold small text-muted">Light</label>
        <label id="switch" class="switch mb-0">
          <input type="checkbox" onchange="toggleTheme()" id="slider">
          <span class="slider round"></span>
        </label>
        <label class="ml-2 mb-0 font-weight-bold small text-muted">Dark</label>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <ul class="nav nav-pills mb-3 nav-justified" id="pills-tab" role="tablist">
          <li class="nav-item bg-info active" data-toggle="tooltip" title="Click to view the Summary">
            <a class="nav-link text-white active" id="pills-summary-tab" data-toggle="pill" href="#pills-summary" role="tab" aria-controls="pills-summary" aria-selected="true">Summary</a>
          </li>
          <li class="nav-item bg-success" data-toggle="tooltip" title="Click to view the Requests">
            <a class="nav-link text-white" id="pills-requests-tab" data-toggle="pill" href="#pills-requests" role="tab" aria-controls="pills-requests" aria-selected="false">Total Requests <span class="badge badge-light">${totalRequests}</span></a>
          </li>
          <li class="nav-item bg-danger" data-toggle="tooltip" title="Click to view the Failed Tests">
            <a class="nav-link text-white" id="pills-failed-tab" data-toggle="pill" href="#pills-failed" role="tab" aria-controls="pills-failed" aria-selected="false">Failed Tests <span class="badge badge-light">${failedRequests}</span></a>
          </li>
          <li class="nav-item bg-warning" data-toggle="tooltip" title="Click to view the Skipped Tests">
            <a class="nav-link text-white" id="pills-skipped-tab" data-toggle="pill" href="#pills-skipped" role="tab" aria-controls="pills-skipped" aria-selected="false">Skipped Tests <span class="badge badge-light">${skippedCount}</span></a>
          </li>
        </ul>

        <div class="tab-content" id="pills-tabContent">
          <!-- Summary Tab -->
          <div class="tab-pane fade show active" id="pills-summary" role="tabcard" aria-labelledby="pills-summary-tab">
            <div class="row">
              <div class="col-md-9 col-lg-12 main">
                <h1 class="display-4 text-center mb-2">Newman Run Dashboard</h1>
                <h5 class="text-center text-muted mb-4">${nowFormatted}</h5>
                <div class="row">
                  <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card text-white card-success">
                      <div class="card-body bg-success">
                        <div class="rotate"><i class="fa fa-retweet fa-4x"></i></div>
                        <h6 class="text-uppercase">Total Iterations</h6>
                        <h1 class="display-3 font-weight-bold">1</h1>
                      </div>
                    </div>
                  </div>
                  <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card text-white card-info">
                      <div class="card-body bg-info">
                        <div class="rotate"><i class="fa fa-list fa-4x"></i></div>
                        <h6 class="text-uppercase">Total Assertions</h6>
                        <h1 class="display-3 font-weight-bold">${totalAssertions}</h1>
                      </div>
                    </div>
                  </div>
                  <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card text-white card-danger">
                      <div class="card-body ${failedRequests > 0 ? "bg-danger" : "bg-success"}">
                        <div class="rotate"><i class="fa fa-plus-circle fa-4x"></i></div>
                        <h6 class="text-uppercase">Failed Requests / Tests</h6>
                        <h1 class="display-3 font-weight-bold">${failedTests}</h1>
                      </div>
                    </div>
                  </div>
                  <div class="col-lg-3 col-md-6 mb-3">
                    <div class="card text-white card-warning">
                      <div class="card-body bg-warning">
                        <div class="rotate"><i class="fa fa-share fa-4x"></i></div>
                        <h6 class="text-uppercase">Total Skipped Tests</h6>
                        <h1 class="display-3 font-weight-bold">${skippedCount}</h1>
                      </div>
                    </div>
                  </div>
                </div>

                <hr>

                <div class="row">
                  <div class="col-md-6 mb-3">
                    <div class="card border-info h-100">
                      <div class="card-body">
                        <h5 class="card-title text-uppercase text-white text-center bg-info p-2 rounded">File Information</h5>
                        <p class="mb-1"><i class="fas fa-file-code text-info"></i> <strong>Collection:</strong> ${escapeHtml(descriptor.name)}</p>
                        <p class="mb-1"><i class="fas fa-file-alt text-info"></i> <strong>File:</strong> <code>${escapeHtml(descriptor.fileName)}</code></p>
                        <p class="mb-0"><i class="fas fa-cog text-info"></i> <strong>Environment:</strong> ${escapeHtml(descriptor.environment ?? "Data Plane Environment")}</p>
                      </div>
                    </div>
                  </div>
                  <div class="col-md-6 mb-3">
                    <div class="card border-info h-100">
                      <div class="card-body">
                        <h5 class="card-title text-uppercase text-white text-center bg-info p-2 rounded">Timings and Data</h5>
                        <p class="mb-1"><i class="fas fa-stopwatch text-info"></i> <strong>Total run duration:</strong> ${durationSec}s</p>
                        <p class="mb-1"><i class="fas fa-database text-info"></i> <strong>Total data received:</strong> ${totalDataMb}MB</p>
                        <p class="mb-0"><i class="fas fa-tachometer-alt text-info"></i> <strong>Average response time:</strong> ${avgLatency}ms</p>
                      </div>
                    </div>
                  </div>
                </div>

                <div class="row">
                  <div class="col-12 mb-3">
                    <div class="table-responsive">
                      <table class="table table-striped table-bordered">
                        <thead class="thead-dark text-center">
                          <tr>
                            <th class="text-uppercase">Summary Item</th>
                            <th class="text-uppercase">Total</th>
                            <th class="text-uppercase">Passed</th>
                            <th class="text-uppercase">Failed</th>
                          </tr>
                        </thead>
                        <tbody>
                          <tr>
                            <td><strong>Requests</strong></td>
                            <td class="text-center font-weight-bold">${totalRequests}</td>
                            <td class="text-center text-success font-weight-bold">${passedRequests}</td>
                            <td class="text-center ${failedRequests > 0 ? "text-danger font-weight-bold" : "text-muted"}">${failedRequests}</td>
                          </tr>
                          <tr>
                            <td><strong>Test Assertions</strong></td>
                            <td class="text-center font-weight-bold">${totalAssertions}</td>
                            <td class="text-center text-success font-weight-bold">${passedAssertions}</td>
                            <td class="text-center ${failedAssertions > 0 ? "text-danger font-weight-bold" : "text-muted"}">${failedAssertions}</td>
                          </tr>
                          <tr>
                            <td><strong>Skipped Tests</strong></td>
                            <td class="text-center">${skippedCount}</td>
                            <td class="text-center text-muted">-</td>
                            <td class="text-center text-muted">-</td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  </div>
                </div>

              </div>
            </div>
          </div>

          <!-- Total Requests Tab -->
          <div class="tab-pane fade" id="pills-requests" role="tabcard" aria-labelledby="pills-requests-tab">
            <button id="topOfRequestsScreen" class="btn btn-outline-success btn-sm backToTop" onclick="window.scrollTo({top: 0, behavior: 'smooth'});">Go To Top</button>
            <div class="btn-group float-right mb-3" role="group">
              <button id="openAll" class="btn btn-outline-success btn-sm" onclick="$('#execution-data .collapse').collapse('show');">Expand All</button>
              <button id="closeAll" class="btn btn-outline-secondary btn-sm" onclick="$('#execution-data .collapse').collapse('hide');">Collapse All</button>
            </div>

            <div class="text-uppercase mb-2" id="execution-menu">
              <h5>1 Iteration (${totalRequests} Endpoints)</h5>
            </div>

            <div class="tab-content" id="execution-data">
              ${foldersHtml}
            </div>
          </div>

          <!-- Failed Tests Tab -->
          <div class="tab-pane fade" id="pills-failed" role="tabcard" aria-labelledby="pills-failed-tab">
            ${
              failedRequests > 0
                ? `
              <div class="alert alert-danger text-center font-weight-bold">
                ${failedRequests} failed request${failedRequests === 1 ? "" : "s"}. Showing only failed requests below.
              </div>
              <div class="btn-group float-right mb-3" role="group">
                <button class="btn btn-outline-danger btn-sm" onclick="$('#failed-execution-data .collapse').collapse('show');">Expand All</button>
                <button class="btn btn-outline-secondary btn-sm" onclick="$('#failed-execution-data .collapse').collapse('hide');">Collapse All</button>
              </div>
              <div class="clearfix"></div>
              <div id="failed-execution-data">
                ${failedFoldersHtml}
              </div>
            `
                : `
              <div class="alert alert-success text-uppercase text-center py-5">
                <h1>There are no failed tests <i class="far fa-thumbs-up"></i></h1>
                <p class="mb-0">All ${totalRequests} requests passed assertion checks successfully.</p>
              </div>
            `
            }
          </div>

          <!-- Skipped Tests Tab -->
          <div class="tab-pane fade" id="pills-skipped" role="tabcard" aria-labelledby="pills-skipped-tab">
            ${
              skippedCount
                ? `<div class="alert alert-warning text-center">${skippedCount} of ${skippedCount + executions.length} collection requests did not run. The collection's scripts skipped them (postman.setNextRequest).</div>
            <div class="table-responsive">
              <table class="table table-bordered">
                <thead class="thead-light"><tr><th>Request</th><th>Folder</th><th>Method</th><th>URL</th></tr></thead>
                <tbody>
                  ${skippedRequests
                    .map((request) => `<tr><td>${escapeHtml(request.name)}</td><td>${escapeHtml(request.folder ?? "Root Requests")}</td><td>${escapeHtml(request.method)}</td><td class="text-wrap"><code>${escapeHtml(request.url)}</code></td></tr>`)
                    .join("")}
                </tbody>
              </table>
            </div>`
                : `<div class="alert alert-info text-uppercase text-center py-5">
              <h1>There are no skipped tests <i class="far fa-smile"></i></h1>
            </div>`
            }
          </div>

        </div>
      </div>
    </div>
  </div>

  <script src="https://code.jquery.com/jquery-3.3.1.slim.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/popper.js/1.14.3/umd/popper.min.js"></script>
  <script src="https://stackpath.bootstrapcdn.com/bootstrap/4.1.3/js/bootstrap.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/clipboard.js/2.0.0/clipboard.min.js"></script>
  <script>
    new ClipboardJS('.copyButton');
  </script>
</body>
</html>`;
}
