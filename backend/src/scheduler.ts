import type winston from "winston";
import type { CollectionController } from "./controllers/collectionController";
import type { Mailer } from "./mailer";
import type { RunManager } from "./runner";
import { INTERVAL_MS, type SettingsStore, type RerunInterval } from "./repositories/settingsStore";

export class AutoRerunScheduler {
  private timer: NodeJS.Timeout | null = null;
  private running = false;

  constructor(
    private readonly settings: SettingsStore,
    private readonly runner: RunManager,
    private readonly controller: CollectionController,
    private readonly mailer: Mailer,
    private readonly logger: winston.Logger
  ) {}

  start(runNow = false): void {
    this.stop();
    const { interval } = this.settings.get();
    if (interval === "off") {
      this.logger.info("Auto re-run is off");
      return;
    }
    const ms = (interval in INTERVAL_MS)
      ? INTERVAL_MS[interval as Exclude<RerunInterval, "off">]
      : (15 * 60 * 1000);
    this.timer = setInterval(() => void this.tick(), ms);
    this.logger.info("Auto re-run scheduled", { interval, intervalMs: ms, runNow });
    if (runNow) void this.tick();
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
  }

  private async tick(): Promise<void> {
    if (this.running) {
      this.logger.warn("Skipping auto re-run because the previous one is still in progress");
      return;
    }
    this.running = true;
    const startedAt = new Date();

    try {
      const { collectionIds } = this.settings.get();
      const { waitedFor } = await this.runner.runAllAndWait(collectionIds);
      const completedAt = new Date();
      const data = await this.controller.getDashboardData(collectionIds);
      this.logger.info("Auto re-run completed", {
        durationMs: completedAt.getTime() - startedAt.getTime(),
        failedCollections: data.kpis.failedCollections,
        waitedForAlreadyRunning: waitedFor
      });
      await this.sendEmail("run report", (recipients) => this.mailer.sendRunReport(recipients, { startedAt, completedAt, data }));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.logger.error("Auto re-run could not be executed", { error: message });
      await this.sendEmail("failure alert", (recipients) => this.mailer.sendFailureAlert(recipients, message, new Date()));
    } finally {
      this.running = false;
    }
  }

  private async sendEmail(kind: string, send: (recipients: string[]) => Promise<any>): Promise<void> {
    const { recipients } = this.settings.get();
    if (!this.mailer.isConfigured() || recipients.length === 0) {
      this.logger.warn("Auto re-run email not sent because SMTP or recipients are not configured", { kind });
      return;
    }
    try {
      const info = await send(recipients);
      this.logger.info("Auto re-run email sent", {
        kind,
        recipientCount: recipients.length,
        messageId: info?.messageId,
        response: info?.response
      });
    } catch (error) {
      this.logger.error("Auto re-run email failed to send", { kind, error: error instanceof Error ? error.message : String(error) });
    }
  }
}
