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

// DX-Data-Plane SIRPI matching EXACT names from Postman screenshots
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
            makeReq("400 EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=invalid-id", 400),
            makeReq("401 EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("403 EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 403),
            makeReq("200 (success) EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200)
          ]
        },
        {
          name: "Complex Search",
          item: [
            makeReq("400 Complex Search", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 400, { invalid: "query" }),
            makeReq("401 Complex Search", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 401),
            makeReq("403 Complex Search", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 403),
            makeReq("200 (success) Complex Search", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 200, { geoQuery: { geometry: "Point", coordinates: [77.59, 12.97], maxDistance: 3000 } })
          ]
        },
        {
          name: "Temporal Search",
          item: [
            makeReq("400 Temporal Search", "POST", "https://{{gateway-url}}/gateway/v2/search/temporal", 400, { invalid: "query" }),
            makeReq("401 Temporal Search", "POST", "https://{{gateway-url}}/gateway/v2/search/temporal", 401),
            makeReq("403 Temporal Search", "POST", "https://{{gateway-url}}/gateway/v2/search/temporal", 403),
            makeReq("200 (success) Temporal Search", "POST", "https://{{gateway-url}}/gateway/v2/search/temporal", 200, { timerel: "between", timeAt: "2026-01-01T00:00:00Z", endTimeAt: "2026-06-01T00:00:00Z" })
          ]
        }
      ]
    }
  ]
};

const filePath = path.join(collectionsDir, "DX-Data-Plane SIRPI.postman_collection (3).json");
fs.writeFileSync(filePath, JSON.stringify(dxDataPlane, null, 2), "utf8");
console.log("Successfully synced DX-Data-Plane SIRPI collection with exact Postman hierarchy!");
