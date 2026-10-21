import { readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { z } from "zod";
import type { CollectionDescriptor, PostRunManifest, SkippedFile } from "./types";

export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

const collectionSchema = z.object({
  info: z.object({ name: z.string().optional() }).passthrough(),
  item: z.array(z.unknown())
}).passthrough();

const environmentSchema = z.object({ values: z.array(z.unknown()) }).passthrough();
const postRunSchema = z.object({
  export: z.record(z.string(), z.string()).optional(),
  ignoreFiles: z.array(z.string()).optional(),
  python: z.object({ requirements: z.string().min(1) }).optional(),
  steps: z.array(z.object({
    name: z.string().min(1),
    command: z.string().min(1),
    args: z.array(z.string()),
    shell: z.boolean().optional(),
    env: z.record(z.string(), z.string()).optional()
  }).passthrough())
}).passthrough();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function countRequests(items: unknown): number {
  if (!Array.isArray(items)) return 0;
  return items.reduce((count, item) => {
    if (typeof item !== "object" || item === null || Array.isArray(item)) return count;
    const record = item as Record<string, unknown>;
    return count + (typeof record.request === "object" && record.request !== null && !Array.isArray(record.request) ? 1 : 0) + countRequests(record.item);
  }, 0);
}

export interface Catalog {
  collections: CollectionDescriptor[];
  skipped: SkippedFile[];
}

export async function scanCatalog(collectionsDir: string): Promise<Catalog> {
  const entries = await readdir(collectionsDir, { withFileTypes: true });
  const collections: CollectionDescriptor[] = [];
  const skipped: SkippedFile[] = [];

  for (const entry of entries) {
    if (!entry.isDirectory()) continue;

    const folderName = entry.name;
    const folderPath = path.join(collectionsDir, folderName);
    const postRunPath = path.join(folderPath, "post-run.json");
    const hasPostRunFile = (await readdir(folderPath, { withFileTypes: true })).some((file) => file.isFile() && file.name === "post-run.json");
    let postRunFile: string | null = null;
    try {
      if (!hasPostRunFile) throw Object.assign(new Error("Not found"), { code: "ENOENT" });
      postRunFile = "post-run.json";
      const parsed: unknown = JSON.parse(await readFile(postRunPath, "utf8"));
      const result = postRunSchema.safeParse(parsed);
      if (!result.success) throw new Error(result.error.issues.map((issue) => issue.message).join("; "));
      const postRunManifest = result.data as PostRunManifest;
      for (const step of postRunManifest.steps) {
        if (path.isAbsolute(step.command) || step.command.split(/[\\/]/).includes("..")) {
          throw new Error(`Cleanup command must be a relative path inside its collection folder: ${step.command}`);
        }
        const templates = [...step.args, ...Object.values(step.env ?? {})].flatMap((value) => [...value.matchAll(/\{\{\s*(env\.)?([A-Za-z0-9_.-]+)\s*\}\}/g)].map((match) => ({ prefix: match[1] ?? "", key: match[2] })));
        for (const template of templates) {
          if (!template.prefix && !postRunManifest.export?.[template.key]) {
            throw new Error(`Post-run value "${template.key}" must be declared in export or use the env. prefix.`);
          }
        }
      }
    } catch (error) {
      if (error && typeof error === "object" && "code" in error && error.code === "ENOENT") {
        // A post-run manifest is optional.
      } else {
        skipped.push({ folderName, fileName: "post-run.json", reason: `Invalid post-run manifest: ${error instanceof Error ? error.message : String(error)}` });
        continue;
      }
    }
    const ignoredFiles = new Set<string>();
    if (postRunFile) {
      const manifest = JSON.parse(await readFile(postRunPath, "utf8")) as { ignoreFiles?: unknown };
      if (Array.isArray(manifest.ignoreFiles)) {
        for (const fileName of manifest.ignoreFiles) {
          if (typeof fileName !== "string" || path.basename(fileName) !== fileName || !fileName.toLowerCase().endsWith(".json")) {
            skipped.push({ folderName, fileName: "post-run.json", reason: `Invalid ignored JSON file name: ${String(fileName)}` });
            continue;
          }
          ignoredFiles.add(fileName);
        }
      }
    }
    const files = (await readdir(folderPath, { withFileTypes: true }))
      .filter((file) => file.isFile() && file.name.toLowerCase().endsWith(".json") && file.name !== "post-run.json" && !ignoredFiles.has(file.name));

    const parsedCollections: Array<{ fileName: string; value: z.infer<typeof collectionSchema> }> = [];
    const parsedEnvironments: string[] = [];

    for (const file of files) {
      const filePath = path.join(folderPath, file.name);
      let parsed: unknown;
      try {
        parsed = JSON.parse(await readFile(filePath, "utf8"));
      } catch {
        skipped.push({ folderName, fileName: file.name, reason: "Invalid JSON." });
        continue;
      }

      const collectionResult = collectionSchema.safeParse(parsed);
      if (collectionResult.success) {
        parsedCollections.push({ fileName: file.name, value: collectionResult.data });
        continue;
      }

      if (environmentSchema.safeParse(parsed).success) {
        parsedEnvironments.push(file.name);
        continue;
      }

      skipped.push({ folderName, fileName: file.name, reason: "Not a Postman collection or environment." });
    }

    if (parsedCollections.length !== 1 || parsedEnvironments.length !== 1) {
      skipped.push({
        folderName,
        fileName: folderName,
        reason: `Expected exactly one collection and one environment JSON file; found ${parsedCollections.length} collection(s) and ${parsedEnvironments.length} environment(s).`
      });
      continue;
    }

    const collectionFile = parsedCollections[0].fileName;
    const collection = parsedCollections[0].value;
    const environmentFile = parsedEnvironments[0];
    collections.push({
      id: slugify(folderName),
      folderName,
      folderPath: folderName,
      collectionFile,
      environmentFile,
      postRunFile,
      fileName: collectionFile,
      name: typeof collection.info.name === "string" ? collection.info.name : folderName,
      requestCount: countRequests(collection.item),
      environment: environmentFile,
      collection: collection as Record<string, unknown>,
      parseError: null
    });

  }

  return {
    collections: collections.sort((a, b) => a.folderName.localeCompare(b.folderName)),
    skipped: skipped.sort((a, b) => `${a.folderName ?? ""}/${a.fileName}`.localeCompare(`${b.folderName ?? ""}/${b.fileName}`))
  };
}

export function flattenRequests(collection: Record<string, unknown>): Array<{ folder: string | null; name: string; method: string; url: string }> {
  const flattened: Array<{ folder: string | null; name: string; method: string; url: string }> = [];

  function visit(items: unknown, folders: string[]): void {
    if (!Array.isArray(items)) return;
    for (const item of items) {
      if (!isRecord(item)) continue;
      const name = typeof item.name === "string" ? item.name : "Unnamed request";
      if (isRecord(item.request)) {
        const method = typeof item.request.method === "string" ? item.request.method : "GET";
        let url = "";
        if (typeof item.request.url === "string") {
          url = item.request.url;
        } else if (isRecord(item.request.url) && typeof item.request.url.raw === "string") {
          url = item.request.url.raw;
        }
        flattened.push({ folder: folders.length ? folders.join(" / ") : null, name, method, url });
      }
      visit(item.item, [...folders, name]);
    }
  }

  visit(collection.item, []);
  return flattened;
}
