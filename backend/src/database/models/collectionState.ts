import { DataTypes, Model, type CreationOptional, type InferAttributes, type InferCreationAttributes, type Sequelize } from "sequelize";
import type { CollectionState } from "../../types";

export class CollectionStateModel extends Model<InferAttributes<CollectionStateModel>, InferCreationAttributes<CollectionStateModel>> {
  declare collectionId: string;
  declare state: CollectionState;
  declare createdAt: CreationOptional<Date>;
  declare updatedAt: CreationOptional<Date>;
}

export function initCollectionStateModel(sequelize: Sequelize): typeof CollectionStateModel {
  CollectionStateModel.init(
    {
      collectionId: {
        type: DataTypes.STRING,
        primaryKey: true,
        allowNull: false
      },
      state: {
        type: DataTypes.JSONB,
        allowNull: false
      },
      createdAt: DataTypes.DATE,
      updatedAt: DataTypes.DATE
    },
    {
      sequelize,
      modelName: "CollectionState",
      tableName: "collection_states",
      timestamps: true
    }
  );

  return CollectionStateModel;
}
