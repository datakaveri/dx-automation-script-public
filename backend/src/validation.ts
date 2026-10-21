import type { NextFunction, Request, RequestHandler, Response } from "express";
import { z } from "zod";

const routeParamSchema = z.string().trim().min(1);

export const selectedRunRequestSchema = z
  .object({
    collectionIds: z.array(z.string().trim().min(1)).min(1).max(50),
  })
  .strict()
  .superRefine(({ collectionIds }, context) => {
    const seen = new Set<string>();
    collectionIds.forEach((id, index) => {
      if (seen.has(id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ["collectionIds", index],
          message: `Duplicate collection ID: ${id}`,
        });
      }
      seen.add(id);
    });
  });

export const endpointRunQuerySchema = z
  .object({
    search: z.string().trim().default(""),
    status: z.enum(["all", "passed", "failed"]).default("all"),
    method: z
      .enum(["all", "GET", "POST", "PUT", "PATCH", "DELETE"])
      .default("all"),
    folder: z.string().trim().default("all"),
    page: z.coerce.number().int().min(1).default(1),
    pageSize: z.coerce.number().int().min(1).max(100).default(100),
  })
  .strict();

export const autoRerunSettingsSchema = z
  .object({
    interval: z.enum(["off", "15m", "1h", "24h"]).optional(),
    recipients: z.array(z.email()).min(1).optional(),
    collectionIds: z
      .array(z.string().trim().min(1))
      .min(1)
      .max(50)
      .refine(
        (ids) => new Set(ids).size === ids.length,
        "Collection IDs must be unique.",
      )
      .nullable()
      .optional(),
  })
  .strict();

export function validateRouteParam(name: string): RequestHandler {
  return (request: Request, response: Response, next: NextFunction) => {
    const result = routeParamSchema.safeParse(request.params[name]);
    if (!result.success) {
      response.status(400).json({
        error: {
          code: "VALIDATION_ERROR",
          message: `Invalid ${name}.`,
          details: result.error.flatten(),
        },
      });
      return;
    }
    next();
  };
}
