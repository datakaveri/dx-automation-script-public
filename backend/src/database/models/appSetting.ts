import { DataTypes, Model, type CreationOptional, type InferAttributes, type InferCreationAttributes, type Sequelize } from "sequelize";

export class AppSettingModel extends Model<InferAttributes<AppSettingModel>, InferCreationAttributes<AppSettingModel>> {
  declare key: string;
  declare value: unknown;
  declare createdAt: CreationOptional<Date>;
  declare updatedAt: CreationOptional<Date>;
}

export function initAppSettingModel(sequelize: Sequelize): typeof AppSettingModel {
  AppSettingModel.init(
    {
      key: {
        type: DataTypes.STRING,
        primaryKey: true,
        allowNull: false
      },
      value: {
        type: DataTypes.JSONB,
        allowNull: false
      },
      createdAt: DataTypes.DATE,
      updatedAt: DataTypes.DATE
    },
    {
      sequelize,
      modelName: "AppSetting",
      tableName: "app_settings",
      timestamps: true
    }
  );

  return AppSettingModel;
}
