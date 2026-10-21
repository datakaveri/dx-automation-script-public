import nodemailer, { type Transporter, type SentMessageInfo } from "nodemailer";
import { formatIST, formatISTDate, formatISTTime } from "./formatTime.js";
import type { AppConfig } from "./config/env";
import type { CollectionController } from "./controllers/collectionController";

export type DashboardData = Awaited<ReturnType<CollectionController["getDashboardData"]>>;

export interface RunReport {
  startedAt: Date;
  completedAt: Date;
  data: DashboardData;
}

const STATUS_COLORS: Record<string, string> = {
  passed: "#15803d",
  failed: "#b91c1c",
  error: "#b91c1c"
};

const CELL = "padding:8px 10px;border:1px solid #dedede;text-align:left;font-size:13px;vertical-align:top";

function emailLayout(title: string, subtitle: string, content: string, dashboardUrl: string): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>${escapeHtml(title)}</title></head>
  <body style="margin:0;padding:0;background-color:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#222222">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:20px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:820px;background-color:#ffffff">
        <tr><td>
          <h1 style="margin:0;font-size:21px;line-height:1.3">${escapeHtml(title)}</h1>
          <p style="margin:8px 0 16px;color:#555555;font-size:13px">${escapeHtml(subtitle)}</p>
        </td></tr>
        <tr><td>${content}</td></tr>
        <tr><td style="padding-top:16px">
          <a href="${escapeHtml(dashboardUrl)}" style="color:#245b83;text-decoration:underline;font-size:13px">Open QA dashboard</a>
          <p style="margin:14px 0 0;font-size:12px;line-height:1.6;color:#64748b">Automated by IUDX QA &bull; Times shown in IST</p>
        </td></tr>
      </table>
    </td></tr></table>
  </body></html>`;
}

function escapeHtml(value: string): string {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

export function formatDuration(ms: number): string {
  const totalSeconds = Math.round(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  if (hours > 0) {
    return `${hours}h ${minutes}m ${seconds}s`;
  }
  return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

function formatTime(date: Date): string {
  return formatIST(date);
}

function reportLink(apiUrl: string, collection: DashboardData["collections"][number]): string {
  // Only passed/failed runs are stored in history; errored runs have no Newman report to open.
  if (!collection.runId || (collection.status !== "passed" && collection.status !== "failed")) return "-";
  const url = `${apiUrl.replace(/\/+$/, "")}/api/collections/${encodeURIComponent(collection.id)}/history/${encodeURIComponent(collection.runId)}/report`;
  return `<a href="${escapeHtml(url)}" style="color:#245b83;text-decoration:underline">View report</a>`;
}

export function buildRunReportHtml(report: RunReport, apiUrl: string, dashboardUrl: string): string {
  const { collections, kpis } = report.data;

  const executionDate = formatISTDate(report.startedAt);
  const startTime = formatISTTime(report.startedAt);
  const endTime = formatISTTime(report.completedAt);
  const duration = formatDuration(report.completedAt.getTime() - report.startedAt.getTime());

  const totalCollections = kpis?.totalCollections ?? collections.length;
  const passedCollections = kpis?.passedCollections ?? collections.filter((c) => c.status === "passed").length;
  const failedCollections = kpis?.failedCollections ?? collections.filter((c) => c.status === "failed" || c.status === "error").length;

  const rows = collections
    .map((collection, index) => {
      const color = STATUS_COLORS[collection.status] ?? "#6b7280";
      return `<tr style="background-color:${index % 2 === 0 ? "#ffffff" : "#f8fafc"}">
        <th scope="row" style="${CELL};font-weight:bold;word-break:break-word">${escapeHtml(collection.folderName)}</th>
        <td style="${CELL};color:${color};font-weight:bold">${escapeHtml(collection.status.toUpperCase())}</td>
        <td style="${CELL}">${collection.apiCount}</td>
        <td style="${CELL}">${collection.tests.passed} / ${collection.tests.failed} / ${collection.tests.total}</td>
        <td style="${CELL}">${collection.assertions.failed}</td>
        <td style="${CELL}">${reportLink(apiUrl, collection)}</td>
      </tr>`;
    })
    .join("");

  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>[IUDX QA] Daily API Automation Test Report</title></head>
  <body style="margin:0;padding:0;background-color:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#222222">
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr><td style="padding:24px 20px">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:820px;background-color:#ffffff">
        <tr><td>
          <p style="margin:0 0 12px;font-size:14px;line-height:1.5;color:#222222">Hi Team,</p>
          <p style="margin:0 0 20px;font-size:14px;line-height:1.5;color:#222222">Please find below the summary of the scheduled daily API automation test execution.</p>

          <h1 style="margin:0 0 18px;font-size:20px;line-height:1.3;color:#0f172a;font-weight:bold">Daily API Health Report</h1>

          <h2 style="margin:0 0 8px;font-size:15px;line-height:1.4;color:#172332;font-weight:bold">Execution Details</h2>
          <ul style="margin:0 0 18px;padding-left:20px;font-size:13px;line-height:1.8;color:#334155">
            <li><strong>Date:</strong> ${escapeHtml(executionDate)}</li>
            <li><strong>Start Time:</strong> ${escapeHtml(startTime)} IST</li>
            <li><strong>Completion Time:</strong> ${escapeHtml(endTime)} IST</li>
            <li><strong>Total Duration:</strong> ${escapeHtml(duration)}</li>
          </ul>

          <h2 style="margin:0 0 8px;font-size:15px;line-height:1.4;color:#172332;font-weight:bold">Execution Summary</h2>
          <ul style="margin:0 0 20px;padding-left:20px;font-size:13px;line-height:1.8;color:#334155">
            <li><strong>Total Collections:</strong> ${totalCollections}</li>
            <li><strong>Collections Passed:</strong> ${passedCollections}</li>
            <li><strong>Collections Failed:</strong> ${failedCollections}</li>
          </ul>

          <h2 style="margin:0 0 12px;font-size:15px;line-height:1.4;color:#172332;font-weight:bold">Collection Results</h2>
          <table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;border:1px solid #e5e7eb">
            <tr style="background-color:#e8eef4;color:#334155">
              <th scope="col" style="${CELL}">Collection</th>
              <th scope="col" style="${CELL}">Status</th>
              <th scope="col" style="${CELL}">APIs</th>
              <th scope="col" style="${CELL}">Tests (passed / failed / total)</th>
              <th scope="col" style="${CELL}">Failed assertions</th>
              <th scope="col" style="${CELL}">Report</th>
            </tr>
            ${rows}
          </table>
          <p style="margin:8px 0 0;color:#64748b;font-size:12px;line-height:1.5">Collection errors are included in the failed count. Reports are available for passed and failed test runs.</p>

          <h2 style="margin:24px 0 8px;font-size:15px;line-height:1.4;color:#172332;font-weight:bold">QA Dashboard</h2>
          <p style="margin:0 0 18px;font-size:13px;line-height:1.6">
            <a href="${escapeHtml(dashboardUrl)}" style="color:#245b83;text-decoration:underline;font-weight:bold;font-size:13px" target="_blank" rel="noopener noreferrer">Open QA Dashboard</a>
          </p>

          <p style="margin:0 0 20px;font-size:13px;line-height:1.6;color:#475569">The report is generated automatically after each scheduled execution. Please refer to the QA dashboard for detailed test results and any failures requiring investigation.</p>

          <p style="margin:0 0 4px;font-size:13px;color:#222222">Regards,</p>
          <p style="margin:0;font-size:13px;font-weight:bold;color:#222222">IUDX QA Automation</p>
        </td></tr>
      </table>
    </td></tr></table>
  </body></html>`;
}

export function buildRunReportText(report: RunReport, dashboardUrl: string): string {
  const { collections, kpis } = report.data;

  const executionDate = formatISTDate(report.startedAt);
  const startTime = formatISTTime(report.startedAt);
  const endTime = formatISTTime(report.completedAt);
  const duration = formatDuration(report.completedAt.getTime() - report.startedAt.getTime());

  const totalCollections = kpis?.totalCollections ?? collections.length;
  const passedCollections = kpis?.passedCollections ?? collections.filter((c) => c.status === "passed").length;
  const failedCollections = kpis?.failedCollections ?? collections.filter((c) => c.status === "failed" || c.status === "error").length;

  return `Hi Team,

Please find below the summary of the scheduled daily API automation test execution.

Daily API Health Report

Execution Details
- Date: ${executionDate}
- Start Time: ${startTime} IST
- Completion Time: ${endTime} IST
- Total Duration: ${duration}

Execution Summary
- Total Collections: ${totalCollections}
- Collections Passed: ${passedCollections}
- Collections Failed: ${failedCollections}

Collection Results
(Please view HTML email for the complete collection results table)

QA Dashboard
Open QA Dashboard: ${dashboardUrl}

The report is generated automatically after each scheduled execution. Please refer to the QA dashboard for detailed test results and any failures requiring investigation.

Regards,

IUDX QA Automation`;
}

export class Mailer {
  private readonly transporter: Transporter | null;

  constructor(private readonly config: AppConfig) {
    this.transporter = config.SMTP_HOST
      ? nodemailer.createTransport({
          host: config.SMTP_HOST,
          port: config.SMTP_PORT,
          secure: config.SMTP_PORT === 465,
          auth: config.SMTP_USER ? { user: config.SMTP_USER, pass: config.SMTP_PASS } : undefined
        })
      : null;
  }

  isConfigured(): boolean {
    return this.transporter !== null;
  }

  async verifyConnection(): Promise<{ success: boolean; message: string }> {
    if (!this.transporter) {
      return { success: false, message: "SMTP is not configured (SMTP_HOST is missing)." };
    }
    try {
      await this.transporter.verify();
      return { success: true, message: "SMTP connection and credentials verified successfully." };
    } catch (error) {
      return {
        success: false,
        message: error instanceof Error ? error.message : String(error)
      };
    }
  }

  async sendTestEmail(recipients: string[]): Promise<SentMessageInfo> {
    const subject = "[IUDX QA] Email Functionality Verification Test";
    const dateStr = formatIST(new Date());
    const html = `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>IUDX QA Email Test</title></head>
    <body style="margin:0;padding:20px;background-color:#ffffff;font-family:Arial,Helvetica,sans-serif;color:#222222">
      <div style="max-width:600px;border:1px solid #e2e8f0;border-radius:8px;padding:24px;background:#f8fafc">
        <h2 style="margin:0 0 12px;color:#0f172a;font-size:18px">IUDX QA Backend &mdash; Email Verification Test</h2>
        <p style="margin:0 0 16px;font-size:14px;color:#334155;line-height:1.5">This test email confirms that the IUDX QA backend SMTP configuration and delivery pipeline are working properly.</p>
        <ul style="margin:0 0 16px;padding-left:20px;font-size:13px;color:#475569;line-height:1.8">
          <li><strong>Timestamp:</strong> ${dateStr}</li>
          <li><strong>Recipients:</strong> ${escapeHtml(recipients.join(", "))}</li>
          <li><strong>Status:</strong> Active &amp; Verified</li>
        </ul>
        <p style="margin:0;font-size:12px;color:#64748b">No API collection runs or scheduled cycles were triggered for this test.</p>
      </div>
    </body></html>`;
    const text = `IUDX QA Backend - Email Verification Test\n\nThis test email confirms that the IUDX QA backend SMTP configuration and delivery pipeline are working properly.\n\nTimestamp: ${dateStr}\nRecipients: ${recipients.join(", ")}\nStatus: Active & Verified\n\nNo API collection runs or scheduled cycles were triggered for this test.`;
    return this.send(recipients, subject, html, text);
  }

  async sendRunReport(recipients: string[], report: RunReport): Promise<SentMessageInfo> {
    const subject = "[IUDX QA] Daily API Automation Test Report";
    const html = buildRunReportHtml(report, this.config.PUBLIC_API_URL, this.config.DASHBOARD_URL);
    const text = buildRunReportText(report, this.config.DASHBOARD_URL);
    return this.send(recipients, subject, html, text);
  }

  async sendFailureAlert(recipients: string[], errorMessage: string, failedAt: Date): Promise<SentMessageInfo> {
    const html = emailLayout("Scheduled run interrupted", "The automatic test cycle needs attention", `<div style="font-size:14px;line-height:1.7">
      <p>The scheduled re-run failed to start or complete at ${formatTime(failedAt)}.</p>
      <p><b>Error:</b> ${escapeHtml(errorMessage)}</p>
      <p>This is a scheduler/infrastructure problem, not a test failure. Please check the server logs.</p>
    </div>`, this.config.DASHBOARD_URL);
    return this.send(recipients, "[IUDX QA] Auto re-run could not be executed", html);
  }

  private async send(recipients: string[], subject: string, html: string, text?: string): Promise<SentMessageInfo> {
    if (!this.transporter) throw new Error("SMTP is not configured.");
    return this.transporter.sendMail({
      from: this.config.MAIL_FROM ?? this.config.SMTP_USER,
      to: recipients.join(", "),
      subject,
      html,
      text
    });
  }
}
