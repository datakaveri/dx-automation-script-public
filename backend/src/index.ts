import dotenv from "dotenv";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createApp } from "./app";
import { loadConfig } from "./config/env";
import { CollectionController } from "./controllers/collectionController";
import { SettingsController } from "./controllers/settingsController";
import { createDatabase } from "./database/client";
import { scanCatalog } from "./catalog";
import { RunManager } from "./runner";
import { SequelizeResultsStore } from "./repositories/sequelizeResultsStore";
import { SettingsStore } from "./repositories/settingsStore";
import { createLogger } from "./logger";
import { Mailer } from "./mailer";
import { AutoRerunScheduler } from "./scheduler";

const serverRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
dotenv.config({ path: path.join(serverRoot, ".env"), override: false });

const logger = createLogger(process.env.NODE_ENV ?? "development");
let config;
try {
  config = loadConfig(process.env);
} catch (error) {
  logger.error("Invalid environment configuration", { error: error instanceof Error ? error.message : error });
  throw error;
}

const collectionsDir = path.resolve(serverRoot, config.COLLECTIONS_DIR);
const database = createDatabase(config.DATABASE_URL);
if (config.DB_SYNC === "alter") await database.sequelize.sync({ alter: true });

const store = new SequelizeResultsStore(database.collectionStateModel);
await store.load();

const loadCatalog = () => scanCatalog(collectionsDir);

// Runs only live inside a server process, so anything still marked "running" at startup
// was interrupted by a restart or deploy. Mark it as error so it isn't skipped forever.
for (const collection of (await loadCatalog()).collections) {
  if (store.get(collection.id).status === "running") {
    logger.warn("Collection run was interrupted by a restart; marking it as error", { collectionId: collection.id });
    await store.complete(collection.id, "error", null);
  }
}

const runner = new RunManager(collectionsDir, loadCatalog, store, logger);
const controller = new CollectionController(loadCatalog, store, runner, config.DASHBOARD_URL);
const defaultRecipients = config.QA_RECIPIENTS.split(",").map((email) => email.trim()).filter(Boolean);
const settingsStore = new SettingsStore(database.appSettingModel, defaultRecipients);
try {
  await settingsStore.load();
} catch (error) {
  logger.error("Could not load auto re-run settings; using defaults (auto re-run off). Check that the app_settings table exists.", {
    error: error instanceof Error ? error.message : String(error)
  });
}

const mailer = new Mailer(config);
if (!mailer.isConfigured()) logger.warn("SMTP is not configured; auto re-run emails will not be sent");
const scheduler = new AutoRerunScheduler(settingsStore, runner, controller, mailer, logger);
const settingsController = new SettingsController(settingsStore, scheduler, loadCatalog);

const corsOrigins = config.CORS_ORIGINS.split(",").map((origin) => origin.trim()).filter(Boolean);
const app = createApp(controller, logger, corsOrigins, settingsController);

app.listen(config.PORT, () => {
  logger.info("Server listening", { port: config.PORT });
  scheduler.start(config.AUTO_RERUN_ON_STARTUP);
});
