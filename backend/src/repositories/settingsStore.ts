import type { ModelStatic } from "sequelize";
import { AppSettingModel } from "../database/models/appSetting";

export type RerunInterval = "off" | "15m" | "1h" | "24h";

export const INTERVAL_MS: Record<Exclude<RerunInterval, "off">, number> = {
  "15m": 15 * 60 * 1000,
  "1h": 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
};

export interface AutoRerunSettings {
  interval: RerunInterval;
  recipients: string[];
  collectionIds: string[] | null;
}

const SETTINGS_KEY = "auto-rerun";

export class SettingsStore {
  private settings: AutoRerunSettings;

  constructor(
    private readonly model: ModelStatic<AppSettingModel>,
    defaultRecipients: string[],
  ) {
    this.settings = {
      interval: "off",
      recipients: defaultRecipients,
      collectionIds: null,
    };
  }

  async load(): Promise<void> {
    const row = await this.model.findByPk(SETTINGS_KEY);
    if (row)
      this.settings = {
        ...this.settings,
        ...(row.value as Partial<AutoRerunSettings>),
      };
  }

  get(): AutoRerunSettings {
    return {
      ...this.settings,
      recipients: [...this.settings.recipients],
      collectionIds: this.settings.collectionIds
        ? [...this.settings.collectionIds]
        : null,
    };
  }

  async update(
    changes: Partial<AutoRerunSettings>,
  ): Promise<AutoRerunSettings> {
    const next = { ...this.settings, ...changes };
    await this.model.upsert({ key: SETTINGS_KEY, value: next });
    this.settings = next;
    return this.get();
  }
}
