import type { Request, Response } from "express";
import type { SettingsStore } from "../repositories/settingsStore";
import type { AutoRerunScheduler } from "../scheduler";
import { autoRerunSettingsSchema } from "../validation";
import type { Catalog } from "../catalog";

export class SettingsController {
  constructor(
    private readonly settings: SettingsStore,
    private readonly scheduler: AutoRerunScheduler,
    private readonly loadCatalog: () => Promise<Catalog>
  ) {}

  getAutoRerun(_request: Request, response: Response): void {
    response.json({ data: this.settings.get() });
  }

  async updateAutoRerun(request: Request, response: Response): Promise<void> {
    const parsed = autoRerunSettingsSchema.safeParse(request.body);
    if (!parsed.success) {
      response.status(400).json({
        error: {
          code: "VALIDATION_ERROR",
          message: "Invalid auto re-run settings.",
          details: parsed.error.flatten()
        }
      });
      return;
    }

    if (parsed.data.collectionIds) {
      const available = new Set((await this.loadCatalog()).collections.map((collection) => collection.id));
      const missing = parsed.data.collectionIds.filter((id) => !available.has(id));
      if (missing.length) {
        response.status(400).json({ error: { code: "VALIDATION_ERROR", message: "Unknown or unavailable collection IDs.", details: { collectionIds: missing } } });
        return;
      }
    }
    const updated = await this.settings.update(parsed.data);
    this.scheduler.start();
    response.json({ data: updated });
  }
}
