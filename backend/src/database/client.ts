import { Sequelize } from "sequelize";
import { initCollectionStateModel } from "./models/collectionState";
import { initAppSettingModel } from "./models/appSetting";

export function createDatabase(databaseUrl: string) {
  const sequelize = new Sequelize(databaseUrl, { logging: false });
  const collectionStateModel = initCollectionStateModel(sequelize);
  const appSettingModel = initAppSettingModel(sequelize);

  return { sequelize, collectionStateModel, appSettingModel };
}
