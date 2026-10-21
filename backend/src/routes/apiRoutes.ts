import { Router } from "express";
import { CollectionController } from "../controllers/collectionController";
import { SettingsController } from "../controllers/settingsController";
import { validateRouteParam } from "../validation";
import { requireRealmRole } from "../authentication";

export function createApiRouter(
  controller: CollectionController,
  settingsController: SettingsController,
): Router {
  const router = Router();
  const requireQaAdmin = requireRealmRole("qa_admin");
  router.param("id", validateRouteParam("id"));
  router.param("runId", validateRouteParam("runId"));

  router.get("/collections", controller.list.bind(controller));
  router.get("/skipped", controller.skipped.bind(controller));
  router.get("/summary", controller.summary.bind(controller));
  router.get("/dashboard", controller.dashboard.bind(controller));
  router.get(
    "/collections/:id/runs/latest",
    controller.latestRun.bind(controller),
  );
  router.get("/collections/:id/result", controller.result.bind(controller));
  router.get("/collections/:id/history", controller.history.bind(controller));
  router.get(
    "/collections/:id/history/:runId/report",
    controller.historyReport.bind(controller),
  );
  router.get("/collections/:id/report", controller.report.bind(controller));
  router.get("/runs/:runId", controller.runStatus.bind(controller));
  router.post(
    "/collections/:id/run",
    requireQaAdmin,
    controller.runOne.bind(controller),
  );
  router.post(
    "/run-selected",
    requireQaAdmin,
    controller.runSelected.bind(controller),
  );
  router.post("/run-all", requireQaAdmin, controller.runAll.bind(controller));

  router.get(
    "/settings/auto-rerun",
    settingsController.getAutoRerun.bind(settingsController),
  );
  router.put(
    "/settings/auto-rerun",
    requireQaAdmin,
    settingsController.updateAutoRerun.bind(settingsController),
  );

  return router;
}
