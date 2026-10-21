import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const collectionsDir = path.resolve(__dirname, "../collections");

function makeTest(statusCode = 200, name = "Status code is " + statusCode) {
  return [
    {
      listen: "test",
      script: {
        exec: [
          `pm.test("${name}", function () {`,
          `    pm.response.to.have.status(${statusCode});`,
          `});`
        ],
        type: "text/javascript"
      }
    }
  ];
}

function makeReq(name, method, rawUrl, expectedStatus = 200, bodyObj = null, customHeaders = []) {
  const cleanUrl = rawUrl.replace(/^https?:\/\//, "");
  const parts = cleanUrl.split("/");
  const host = [parts[0]];
  const pathParts = parts.slice(1).map(p => p.split("?")[0]);
  
  const headers = [
    { key: "Content-Type", value: "application/json" },
    { key: "Authorization", value: "Bearer {{consumerToken}}" },
    ...customHeaders
  ];

  const item = {
    name,
    event: makeTest(expectedStatus, `Status code is ${expectedStatus}`),
    request: {
      method,
      header: headers,
      url: {
        raw: rawUrl,
        protocol: "https",
        host,
        path: pathParts
      }
    },
    response: []
  };

  if (bodyObj) {
    item.request.body = {
      mode: "raw",
      raw: typeof bodyObj === "string" ? bodyObj : JSON.stringify(bodyObj, null, 2)
    };
  }

  return item;
}

// 1. DX-Data-Plane SIRPI
const dxDataPlane = {
  info: {
    _postman_id: "cb423c0e-f868-45ce-8980-b75add3428c4",
    name: "DX-Data-Plane SIRPI",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
    _exporter_id: "44150667"
  },
  item: [
    {
      name: "Token",
      item: [
        makeReq("Consumer Token", "POST", "https://{{keycloak-url}}/auth/realms/iudx-v2/protocol/openid-connect/token", 200),
        makeReq("Generate Client id and Client Secret", "POST", "https://{{data-plane-url}}/controlplane/iudx/v2/auth/client", 200, { client_name: "sirpi-client", roles: ["consumer"] }),
        makeReq("AAA Token", "POST", "https://{{data-plane-url}}/controlplane/iudx/v2/auth/token", 200, { itemId: "resource-01", itemType: "resource", role: "consumer" })
      ]
    },
    {
      name: "NGSILD_SERVER",
      item: [
        {
          name: "NGSILD",
          item: [
            makeReq("200 - temporal", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?timerel=between&timeAt=2021-03-28T21:43:44+05:30&timeproperty=observationDateTime&endTimeAt=2026-03-29T21:43:44+05:30&offset=0&limit=5&id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a", 200),
            makeReq("200 - temporal Count", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?timerel=between&timeAt=2021-03-28T21:43:44+05:30&timeproperty=observationDateTime&endTimeAt=2026-03-29T21:43:44+05:30&offset=0&limit=5&id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a&count=true", 200),
            makeReq("400 - temporal", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?timerel=between&timeAt=2021-03-28T21:43:44+05:30&timeproperty=observationDateTime&offset=0&limit=5&id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a", 400),
            makeReq("401 - temporal", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?timerel=between&timeAt=2021-03-28T21:43:44+05:30&timeproperty=observationDateTime&endTimeAt=2026-03-29T21:43:44+05:30&offset=0&limit=5&id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a", 401),
            makeReq("404 - temporal", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?timerel=between&timeAt=2021-03-28T21:43:44+05:30&timeproperty=observationDateTime&endTimeAt=2026-03-29T21:43:44+05:30&offset=0&limit=5&id=00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 - entities", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200),
            makeReq("200 - entities Count", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&count=true", 200),
            makeReq("400 - entities", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=invalid-id-format-123", 400),
            makeReq("401 - entities", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("404 - entities", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 - entitiesOperation", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/query?limit=2&format=simplified", 200, { entities: [{ id: "51ef8ad1-a6b4-4321-8863-acd58df3ea5c" }] }),
            makeReq("200 - entitiesOperation Count", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/query?limit=2&format=simplified&count=true", 200, { entities: [{ id: "51ef8ad1-a6b4-4321-8863-acd58df3ea5c" }] }),
            makeReq("400 - entitiesOperation", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/query?limit=2&format=simplified", 400),
            makeReq("401 - entitiesOperation", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/query?limit=2&format=simplified", 401),
            makeReq("404 - entitiesOperation", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/query?limit=2&format=simplified", 404),
            makeReq("200 - temporalOperations", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entityOperations/query?offset=0&limit=2", 200, { entities: [{ id: "04bf3f4e-d74e-11f0-a2ba-eb694a98984a" }] }),
            makeReq("200 - temporalOperations Count", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entityOperations/query?count=true", 200, { entities: [{ id: "04bf3f4e-d74e-11f0-a2ba-eb694a98984a" }] }),
            makeReq("400 - temporalOperations", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entityOperations/query", 400),
            makeReq("401 - temporalOperations", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entityOperations/query", 401),
            makeReq("404 - temporalOperations", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entityOperations/query", 404)
          ]
        },
        {
          name: "Latest Data",
          item: [
            makeReq("400 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/invalid-id-format-123", 400),
            makeReq("401 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("403 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 403),
            makeReq("404 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 (success) Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200)
          ]
        },
        {
          name: "Search Data",
          item: [
            makeReq("400 Post Search", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/search", 400, { invalid: "search" }),
            makeReq("401 Post Search", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/search", 401),
            makeReq("403 Post Search", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/search", 403),
            makeReq("200 (success) Post Search", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/search", 200, { id: "51ef8ad1-a6b4-4321-8863-acd58df3ea5c" })
          ]
        },
        {
          name: "Download Data",
          item: [
            makeReq("400 Download", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download", 400, { invalid: "download" }),
            makeReq("401 Download", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download", 401),
            makeReq("403 Download", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download", 403),
            makeReq("404 Download", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download", 404, { id: "00000000-0000-0000-0000-000000000000" }),
            makeReq("200 (success) Download", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download", 200, { id: "51ef8ad1-a6b4-4321-8863-acd58df3ea5c" }),
            makeReq("400 GET DOWNLOAD", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=invalid-id", 400),
            makeReq("401 GET DOWNLOAD", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("403 GET DOWNLOAD", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 403),
            makeReq("404 GET DOWNLOAD", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 (success) GET DOWNLOAD", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200)
          ]
        },
        {
          name: "Using Appid",
          item: [
            makeReq("01 — Entities GET", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&appid=smart-city-portal", 200)
          ]
        }
      ]
    },
    {
      name: "GATEWAY SERVER",
      item: [
        {
          name: "EntitiesSearch",
          item: [
            makeReq("200 (success) - Simple Attr search V1", "GET", "https://{{gateway-url}}/gateway/v1/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200),
            makeReq("400 (success) - Simple Attr search V1", "GET", "https://{{gateway-url}}/gateway/v1/search/entities?id=invalid-id", 400),
            makeReq("401 (success) - Simple Attr search V1", "GET", "https://{{gateway-url}}/gateway/v1/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("404 (success) - Simple Attr search V1", "GET", "https://{{gateway-url}}/gateway/v1/search/entities?id=00000000-0000-0000-0000-000000000000", 404)
          ]
        },
        {
          name: "Complex Search",
          item: [
            makeReq("200 (success) - Attribute Search V1", "POST", "https://{{gateway-url}}/gateway/v1/search/complex", 200, { geoQuery: { geometry: "Point", coordinates: [77.59, 12.97], maxDistance: 3000 } }),
            makeReq("400 (success) - Attribute Search V1", "POST", "https://{{gateway-url}}/gateway/v1/search/complex", 400, { invalid: "query" }),
            makeReq("401 (success) - Attribute Search V1", "POST", "https://{{gateway-url}}/gateway/v1/search/complex", 401),
            makeReq("404 (success) - Attribute Search V1", "POST", "https://{{gateway-url}}/gateway/v1/search/complex", 404)
          ]
        },
        {
          name: "Temporal Search",
          item: [
            makeReq("200 (success) - temporal (between) V1", "POST", "https://{{gateway-url}}/gateway/v1/search/temporal", 200, { timerel: "between", timeAt: "2026-01-01T00:00:00Z", endTimeAt: "2026-06-01T00:00:00Z" }),
            makeReq("200 (success) - temporal v2", "POST", "https://{{gateway-url}}/gateway/v2/search/temporal", 200, { timerel: "after", timeAt: "2026-01-01T00:00:00Z" }),
            makeReq("200 (success) temporal (before) v1", "GET", "https://{{gateway-url}}/gateway/v1/search/temporal?timerel=before&timeAt=2026-01-01T00:00:00Z", 200),
            makeReq("200 (success) temporal (before) V2", "GET", "https://{{gateway-url}}/gateway/v2/search/temporal?timerel=before&timeAt=2026-06-01T00:00:00Z", 200)
          ]
        }
      ]
    }
  ]
};

// 2. Files Connect API matching EXACT names from Postman screenshots
const filesConnect = {
  info: {
    _postman_id: "files-connect-001",
    name: "Files Connect API",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Authentication & Authorization",
      item: [
        makeReq("Obtain access token (CosAdmin)", "POST", "https://{{files-url}}/file/v1/auth/token", 200, { role: "CosAdmin", client_id: "admin-client" }),
        makeReq("Obtain access token (Provider)", "POST", "https://{{files-url}}/file/v1/auth/token", 200, { role: "Provider", client_id: "provider-client" }),
        makeReq("Obtain access token (Consumer)", "POST", "https://{{files-url}}/file/v1/auth/token", 200, { role: "Consumer", client_id: "consumer-client" })
      ]
    },
    {
      name: "Databanks",
      item: [
        {
          name: "1. List Files",
          item: [
            makeReq("List files - 200 OK", "POST", "https://{{files-url}}/file/v1/databank/list", 200, { bucket: "databank-01", limit: 20 }),
            makeReq("List files - 401", "POST", "https://{{files-url}}/file/v1/databank/list", 401),
            makeReq("List files - 403", "POST", "https://{{files-url}}/file/v1/databank/list", 403, { bucket: "restricted-databank" })
          ]
        },
        {
          name: "2. Download File",
          item: [
            makeReq("Download file - 200 OK", "POST", "https://{{files-url}}/file/v1/databank/download", 200, { fileId: "f-101" }),
            makeReq("Download file - 404 Not Found", "POST", "https://{{files-url}}/file/v1/databank/download", 404, { fileId: "f-nonexistent" })
          ]
        },
        {
          name: "3. File Metadata",
          item: [
            makeReq("File metadata - 200 OK", "POST", "https://{{files-url}}/file/v1/databank/metadata", 200, { fileId: "f-101" }),
            makeReq("File metadata - 404 Not Found", "POST", "https://{{files-url}}/file/v1/databank/metadata", 404, { fileId: "f-nonexistent" })
          ]
        },
        {
          name: "4. File Preview",
          item: [
            makeReq("File preview - 200 OK", "POST", "https://{{files-url}}/file/v1/databank/preview", 200, { fileId: "f-101", previewBytes: 1024 }),
            makeReq("File preview - 400 Invalid request", "POST", "https://{{files-url}}/file/v1/databank/preview", 400, { previewBytes: -50 }),
            makeReq("File preview - 404 Not Found", "POST", "https://{{files-url}}/file/v1/databank/preview", 404, { fileId: "f-nonexistent" })
          ]
        },
        {
          name: "5. Delete File",
          item: [
            makeReq("Delete file - 200 OK", "POST", "https://{{files-url}}/file/v1/databank/delete", 200, { fileId: "f-101" }),
            makeReq("Delete file - 404 Not Found", "POST", "https://{{files-url}}/file/v1/databank/delete", 404, { fileId: "f-nonexistent" })
          ]
        },
        {
          name: "6. Initiate Upload",
          item: [
            makeReq("Initiate upload - 200 OK (Upload initiated)", "POST", "https://{{files-url}}/file/v1/upload/initiate", 200, { fileName: "dataset.csv", fileSize: 1048576 }),
            makeReq("Initiate upload - 400 Bad Request (Validation error)", "POST", "https://{{files-url}}/file/v1/upload/initiate", 400, {}),
            makeReq("Initiate upload - 415 Unsupported Media Type", "POST", "https://{{files-url}}/file/v1/upload/initiate", 415, { fileName: "malicious.exe", mimeType: "application/x-dosexec" })
          ]
        },
        {
          name: "7. Complete Upload",
          item: [
            makeReq("Complete upload - 200 OK", "PUT", "https://{{files-url}}/file/v1/upload/complete", 200, { uploadId: "upl-01", parts: [] }),
            makeReq("Complete upload - 400 Bad Request", "PUT", "https://{{files-url}}/file/v1/upload/complete", 400, { uploadId: "upl-01", parts: "invalid" })
          ]
        },
        {
          name: "8. Cancel Upload",
          item: [
            makeReq("Cancel upload - 200 OK", "POST", "https://{{files-url}}/file/v1/upload/cancel", 200, { uploadId: "upl-01" }),
            makeReq("Cancel upload - 400 Bad Request", "POST", "https://{{files-url}}/file/v1/upload/cancel", 400, {}),
            makeReq("Cancel upload - 404 Not Found", "POST", "https://{{files-url}}/file/v1/upload/cancel", 404, { uploadId: "upl-nonexistent" })
          ]
        },
        {
          name: "9. Query Access",
          item: [
            makeReq("Query access - 200 OK", "GET", "https://{{files-url}}/file/v1/access/query?fileId=f-101", 200),
            makeReq("Query access - 400 Bad Request", "GET", "https://{{files-url}}/file/v1/access/query", 400),
            makeReq("Query access - 401 Unauthorized", "GET", "https://{{files-url}}/file/v1/access/query?fileId=f-101", 401),
            makeReq("Query access - 403 Forbidden", "GET", "https://{{files-url}}/file/v1/access/query?fileId=f-restricted", 403)
          ]
        }
      ]
    },
    {
      name: "Assets",
      item: [
        makeReq("Create asset - 201 Created", "POST", "https://{{files-url}}/file/v1/assets", 201, { name: "Spatial Raster Layer", format: "GeoTIFF" }),
        makeReq("Create asset - 400 Bad Request", "POST", "https://{{files-url}}/file/v1/assets", 400, {}),
        makeReq("Create asset - 401 Unauthorized", "POST", "https://{{files-url}}/file/v1/assets", 401),
        makeReq("Get asset - 200 OK", "GET", "https://{{files-url}}/file/v1/assets/ast-01", 200),
        makeReq("Get asset - 404 Not Found", "GET", "https://{{files-url}}/file/v1/assets/ast-nonexistent", 404),
        makeReq("Update asset - 200 OK", "PUT", "https://{{files-url}}/file/v1/assets/ast-01", 200, { description: "Updated layer" }),
        makeReq("Update asset - 400 Bad Request", "PUT", "https://{{files-url}}/file/v1/assets/ast-01", 400, { invalid: true }),
        makeReq("Delete asset - 200 OK", "DELETE", "https://{{files-url}}/file/v1/assets/ast-01", 200),
        makeReq("Delete asset - 404 Not Found", "DELETE", "https://{{files-url}}/file/v1/assets/ast-nonexistent", 404),
        makeReq("List assets - 200 OK", "GET", "https://{{files-url}}/file/v1/assets?limit=20", 200),
        makeReq("List assets - 401 Unauthorized", "GET", "https://{{files-url}}/file/v1/assets", 401)
      ]
    }
  ]
};

fs.writeFileSync(path.join(collectionsDir, "DX-Data-Plane SIRPI.postman_collection (3).json"), JSON.stringify(dxDataPlane, null, 2), "utf8");
fs.writeFileSync(path.join(collectionsDir, "Files Connect API.postman_collection.json"), JSON.stringify(filesConnect, null, 2), "utf8");

console.log("Successfully updated DX-Data-Plane SIRPI and Files Connect API collections!");
