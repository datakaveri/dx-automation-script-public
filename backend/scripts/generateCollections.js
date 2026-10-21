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
      raw: JSON.stringify(bodyObj, null, 2)
    };
  }

  return item;
}

// 1. DX-Data-Plane SIRPI
const dxDataPlane = {
  info: {
    _postman_id: "cb423c0e-f868-45ce-8980-b75add3428c4",
    name: "DX-Data-Plane SIRPI",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Token",
      item: [
        makeReq("Consumer Token", "POST", "https://{{keycloak-url}}/auth/realms/iudx-v2/protocol/openid-connect/token", 200),
        makeReq("Generate Client id and Client Secret", "POST", "https://{{data-plane-url}}/controlplane/iudx/v2/auth/client", 200, { client_name: "sirpi-client", roles: ["consumer"] }),
        makeReq("AAA Token", "POST", "https://{{data-plane-url}}/controlplane/iudx/v2/auth/token", 200, { itemId: "resource-01", itemType: "resource", role: "consumer" }),
        makeReq("Introspect Token", "POST", "https://{{keycloak-url}}/auth/realms/iudx-v2/protocol/openid-connect/token/introspect", 200, { token: "{{consumerToken}}" }),
        makeReq("Refresh Consumer Token", "POST", "https://{{keycloak-url}}/auth/realms/iudx-v2/protocol/openid-connect/token", 200, { grant_type: "refresh_token" }),
        makeReq("Revoke Client Token", "POST", "https://{{keycloak-url}}/auth/realms/iudx-v2/protocol/openid-connect/revoke", 200, { token: "{{consumerToken}}" })
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
            makeReq("200 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200),
            makeReq("400 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/invalid-id-format-123", 400),
            makeReq("401 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("404 Get Latest data", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 Get Latest data with attrs", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c?attrs=temperature,humidity", 200),
            makeReq("200 Get Latest data batch", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/batch", 200, { ids: ["51ef8ad1-a6b4-4321-8863-acd58df3ea5c", "04bf3f4e-d74e-11f0-a2ba-eb694a98984a"] }),
            makeReq("400 Get Latest data batch empty", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/batch", 400, { ids: [] }),
            makeReq("401 Get Latest data batch unauth", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/batch", 401),
            makeReq("200 Get Latest data options keyValues", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c?options=keyValues", 200),
            makeReq("200 Get Latest data options sysAttrs", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c?options=sysAttrs", 200),
            makeReq("200 Get Latest data count summary", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/51ef8ad1-a6b4-4321-8863-acd58df3ea5c/count", 200),
            makeReq("404 Get Latest data count non-existent", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v2/entities/00000000-0000-0000-0000-000000000000/count", 404)
          ]
        },
        {
          name: "Search Data",
          item: [
            makeReq("200 Search Data Geo-radius", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=near;maxDistance==2000&geometry=Point&coordinates=[77.5946,12.9716]", 200),
            makeReq("400 Search Data Geo-radius", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=near;maxDistance==invalid&geometry=Point", 400),
            makeReq("401 Search Data Geo-radius", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=near;maxDistance==2000&geometry=Point", 401),
            makeReq("404 Search Data Geo-radius", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=near;maxDistance==1&geometry=Point&coordinates=[0.0,0.0]", 404),
            makeReq("200 Search Data Polygon", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=within&geometry=Polygon&coordinates=[[[77.5,12.9],[77.6,12.9],[77.6,13.0],[77.5,13.0],[77.5,12.9]]]", 200),
            makeReq("200 Search Data Property Filter", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?q=temperature>25;airQualityIndex<100", 200),
            makeReq("200 Search Data Text Search", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?q=deviceModel==~\"AQM-*\"", 200),
            makeReq("200 Search Data Bounding Box", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?georel=within&geometry=Box&coordinates=[[77.5,12.9],[77.6,13.0]]", 200),
            makeReq("400 Search Data Invalid Syntax", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?q=invalid===filter", 400),
            makeReq("200 Search Data Order By", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?q=temperature>0&orderBy=observationDateTime:desc", 200),
            makeReq("200 Search Data Pagination Limit Offset", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?limit=10&offset=20", 200),
            makeReq("401 Search Data Unauthorized", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?limit=10", 401)
          ]
        },
        {
          name: "Download Data",
          item: [
            makeReq("200 Download Data JSON", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=json", 200),
            makeReq("400 Download Data JSON", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=invalid-id-format&format=json", 400),
            makeReq("401 Download Data JSON", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=json", 401),
            makeReq("404 Download Data JSON", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=00000000-0000-0000-0000-000000000000&format=json", 404),
            makeReq("200 Download Data CSV", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=csv", 200),
            makeReq("400 Download Data CSV", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=invalid-id-format&format=csv", 400),
            makeReq("401 Download Data CSV", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=csv", 401),
            makeReq("200 Download Data Parquet", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=parquet", 200),
            makeReq("400 Download Data Parquet", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=invalid-id-format&format=parquet", 400),
            makeReq("401 Download Data Parquet", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=parquet", 401),
            makeReq("200 Download Data Compressed ZIP", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=zip", 200),
            makeReq("401 Download Data Compressed ZIP", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&format=zip", 401),
            makeReq("200 Download Data Date Range", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&from=2026-01-01T00:00:00Z&to=2026-06-01T00:00:00Z", 200),
            makeReq("200 Download Data Resumable Chunk", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/download/chunks?chunkId=chk-001", 200)
          ]
        },
        {
          name: "Subscriptions",
          item: [
            makeReq("200 Create NGSI-LD Subscription", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions", 201, { description: "AQI Monitor Alert", entities: [{ type: "AirQualityObserved" }], notification: { endpoint: { uri: "https://consumer.example.org/notify" } } }),
            makeReq("200 List Subscriptions", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions", 200),
            makeReq("200 Get Subscription by ID", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01", 200),
            makeReq("200 Update Subscription", "PATCH", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01", 204, { description: "Updated Alert" }),
            makeReq("200 Delete Subscription", "DELETE", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01", 204),
            makeReq("400 Create Subscription Bad Payload", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions", 400, {}),
            makeReq("401 Subscription Unauthorized", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions", 401),
            makeReq("404 Subscription Not Found", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:non-existent", 404),
            makeReq("200 Pause Subscription", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01/pause", 200),
            makeReq("200 Resume Subscription", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01/resume", 200),
            makeReq("200 Subscription Notification Test", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01/test-notify", 200),
            makeReq("400 Subscription Expiration Invalid", "PATCH", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/subscriptions/urn:ngsi-ld:Subscription:01", 400, { expiresAt: "invalid-date" })
          ]
        },
        {
          name: "Context Broker Operations",
          item: [
            makeReq("200 Create Entity", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities", 201, { id: "urn:ngsi-ld:Device:001", type: "Device", status: { type: "Property", value: "active" } }),
            makeReq("200 Update Entity Attributes", "PATCH", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/urn:ngsi-ld:Device:001/attrs", 204, { status: { type: "Property", value: "maintenance" } }),
            makeReq("200 Append Entity Attributes", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/urn:ngsi-ld:Device:001/attrs", 204, { firmwareVersion: { type: "Property", value: "2.1.0" } }),
            makeReq("200 Replace Entity", "PUT", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/urn:ngsi-ld:Device:001", 204, { id: "urn:ngsi-ld:Device:001", type: "Device", status: { type: "Property", value: "active" } }),
            makeReq("200 Delete Entity", "DELETE", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/urn:ngsi-ld:Device:001", 204),
            makeReq("400 Create Entity Bad Syntax", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities", 400, { invalid: "payload" }),
            makeReq("401 Entity Write Unauthorized", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities", 401),
            makeReq("404 Delete Non-existent Entity", "DELETE", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities/urn:ngsi-ld:Device:non-existent", 404),
            makeReq("200 Batch Create Entities", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/create", 201, [{ id: "urn:ngsi-ld:Device:002", type: "Device" }]),
            makeReq("200 Batch Upsert Entities", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/upsert", 204, [{ id: "urn:ngsi-ld:Device:002", type: "Device" }]),
            makeReq("200 Batch Update Entities", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/update", 204, [{ id: "urn:ngsi-ld:Device:002", type: "Device" }]),
            makeReq("200 Batch Delete Entities", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/delete", 204, ["urn:ngsi-ld:Device:002"]),
            makeReq("400 Batch Upsert Bad Format", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/upsert", 400, { error: "bad array" }),
            makeReq("401 Batch Delete Unauthorized", "POST", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entityOperations/delete", 401)
          ]
        },
        {
          name: "Using Appid",
          item: [
            makeReq("200 - entities using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&appid=smart-city-portal", 200),
            makeReq("400 - entities using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=invalid-id-format-123&appid=smart-city-portal", 400),
            makeReq("401 - entities using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c&appid=smart-city-portal", 401),
            makeReq("404 - entities using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/entities?id=00000000-0000-0000-0000-000000000000&appid=smart-city-portal", 404),
            makeReq("200 - temporal using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a&appid=smart-city-portal&timerel=between&timeAt=2021-03-28T21:43:44+05:30&endTimeAt=2026-03-29T21:43:44+05:30", 200),
            makeReq("400 - temporal using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a&appid=smart-city-portal", 400),
            makeReq("401 - temporal using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?id=04bf3f4e-d74e-11f0-a2ba-eb694a98984a&appid=smart-city-portal&timerel=between&timeAt=2021-03-28T21:43:44+05:30&endTimeAt=2026-03-29T21:43:44+05:30", 401),
            makeReq("404 - temporal using appid", "GET", "https://{{data-plane-url}}/dataplane/ngsi-ld/v1/temporal/entities?id=00000000-0000-0000-0000-000000000000&appid=smart-city-portal&timerel=between&timeAt=2021-03-28T21:43:44+05:30&endTimeAt=2026-03-29T21:43:44+05:30", 404)
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
            makeReq("200 - EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 200),
            makeReq("400 - EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=invalid-id", 400),
            makeReq("401 - EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=51ef8ad1-a6b4-4321-8863-acd58df3ea5c", 401),
            makeReq("404 - EntitiesSearch", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?id=00000000-0000-0000-0000-000000000000", 404),
            makeReq("200 - Search by Type Filter", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?type=AirQualityMonitor", 200),
            makeReq("200 - Search by Property Selection", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?attrs=temperature,humidity,co2", 200),
            makeReq("200 - Search Entities Pagination", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?limit=25&offset=50", 200),
            makeReq("400 - Search Invalid Limit", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?limit=9999999", 400),
            makeReq("200 - Search Geo Circle Gateway", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?georel=near;maxDistance==5000&geometry=Point&coordinates=[77.59,12.97]", 200),
            makeReq("401 - Search Geo Circle Gateway Unauth", "GET", "https://{{gateway-url}}/gateway/v2/search/entities?georel=near;maxDistance==5000&geometry=Point&coordinates=[77.59,12.97]", 401)
          ]
        },
        {
          name: "Complex Search",
          item: [
            makeReq("200 - ComplexSearch Geo+Temporal", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 200, { geoQuery: { geometry: "Point", coordinates: [77.59, 12.97], maxDistance: 3000 }, temporalQuery: { timerel: "between", timeAt: "2026-01-01T00:00:00Z", endTimeAt: "2026-06-01T00:00:00Z" } }),
            makeReq("400 - ComplexSearch Geo+Temporal", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 400, { invalid: "query" }),
            makeReq("401 - ComplexSearch Geo+Temporal", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 401),
            makeReq("404 - ComplexSearch Geo+Temporal", "POST", "https://{{gateway-url}}/gateway/v2/search/complex", 404, { geoQuery: { geometry: "Point", coordinates: [0, 0], maxDistance: 1 } }),
            makeReq("200 - Multi-Attribute Search", "POST", "https://{{gateway-url}}/gateway/v2/search/attributes", 200, { filters: [{ attribute: "pm25", operator: ">=", value: 50 }, { attribute: "status", operator: "==", value: "active" }] }),
            makeReq("400 - Multi-Attribute Search Bad Operator", "POST", "https://{{gateway-url}}/gateway/v2/search/attributes", 400, { filters: [{ attribute: "pm25", operator: "BAD_OP" }] }),
            makeReq("401 - Multi-Attribute Search Unauth", "POST", "https://{{gateway-url}}/gateway/v2/search/attributes", 401),
            makeReq("200 - Aggregate Spatial Density Search", "POST", "https://{{gateway-url}}/gateway/v2/search/aggregate/spatial", 200, { gridResolutionMeters: 500, bbox: [77.5, 12.9, 77.7, 13.1] }),
            makeReq("200 - Temporal Trend Aggregation", "POST", "https://{{gateway-url}}/gateway/v2/search/aggregate/temporal", 200, { interval: "1h", metrics: ["avg(temperature)", "max(pm10)"] }),
            makeReq("400 - Temporal Trend Aggregation Invalid Interval", "POST", "https://{{gateway-url}}/gateway/v2/search/aggregate/temporal", 400, { interval: "999years" }),
            makeReq("200 - Cross-Domain Federated Query", "POST", "https://{{gateway-url}}/gateway/v2/search/federated", 200, { domains: ["transport", "environment"], matchCriteria: "nearestNeighbor" }),
            makeReq("404 - Cross-Domain Federated Query Missing Domain", "POST", "https://{{gateway-url}}/gateway/v2/search/federated", 404, { domains: ["unknown-domain"] })
          ]
        }
      ]
    }
  ]
};

// 2. Control Plane IUDX
const controlPlane = {
  info: {
    _postman_id: "cp-iudx-001",
    name: "Control Plane IUDX",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Authentication (AAA)",
      item: [
        makeReq("OpenID Configuration Discovery", "GET", "https://{{keycloak-url}}/auth/realms/iudx-v2/.well-known/openid-configuration", 200),
        makeReq("Generate AAA Master Token", "POST", "https://{{control-plane-url}}/auth/v2/token", 200, { client_id: "{{client_id}}", client_secret: "{{client_secret}}", grant_type: "client_credentials" }), // gitleaks:allow
        makeReq("Verify Token Introspection", "POST", "https://{{control-plane-url}}/auth/v2/token/introspect", 200, { token: "{{consumerToken}}" }),
        makeReq("Revoke Access Token", "POST", "https://{{control-plane-url}}/auth/v2/token/revoke", 200, { token: "{{consumerToken}}" }),
        makeReq("Register New User", "POST", "https://{{control-plane-url}}/auth/v2/users", 201, { email: "user@example.org", role: "consumer", name: "IUDX Consumer" }),
        makeReq("Get User Profile", "GET", "https://{{control-plane-url}}/auth/v2/users/me", 200),
        makeReq("Update User Profile", "PATCH", "https://{{control-plane-url}}/auth/v2/users/me", 200, { phone: "+91-9876543210" }),
        makeReq("List User Roles & Permissions", "GET", "https://{{control-plane-url}}/auth/v2/users/me/roles", 200),
        makeReq("Assign Role to User", "POST", "https://{{control-plane-url}}/auth/v2/roles/assign", 200, { userId: "usr-001", role: "data-provider" }),
        makeReq("Revoke Role from User", "DELETE", "https://{{control-plane-url}}/auth/v2/roles/assign/usr-001/data-provider", 204),
        makeReq("Create Client API Key", "POST", "https://{{control-plane-url}}/auth/v2/api-keys", 201, { name: "Dashboard Integration Key", expiryDays: 90 }),
        makeReq("List Active API Keys", "GET", "https://{{control-plane-url}}/auth/v2/api-keys", 200),
        makeReq("Revoke API Key", "DELETE", "https://{{control-plane-url}}/auth/v2/api-keys/key-001", 204),
        makeReq("Rotate Client Secret", "POST", "https://{{control-plane-url}}/auth/v2/client/rotate-secret", 200),
        makeReq("401 AAA Unauthorized Token", "GET", "https://{{control-plane-url}}/auth/v2/users/me", 401),
        makeReq("403 Forbidden Admin Scope Check", "GET", "https://{{control-plane-url}}/auth/v2/admin/audit-logs", 403),
        makeReq("404 User Not Found", "GET", "https://{{control-plane-url}}/auth/v2/users/usr-nonexistent", 404),
        makeReq("400 Bad Token Request Payload", "POST", "https://{{control-plane-url}}/auth/v2/token", 400, {}),
        makeReq("List Tenant Realms", "GET", "https://{{control-plane-url}}/auth/v2/realms", 200),
        makeReq("Switch Active Tenant Realm", "POST", "https://{{control-plane-url}}/auth/v2/realms/switch", 200, { realm: "smart-city-pune" }),
        makeReq("Initiate Password Reset", "POST", "https://{{control-plane-url}}/auth/v2/password/reset-request", 200, { email: "user@example.org" }),
        makeReq("Complete Password Reset", "POST", "https://{{control-plane-url}}/auth/v2/password/reset-confirm", 200, { token: "rst-token", newPassword: "SecurePassword123!" }),
        makeReq("List Active User Sessions", "GET", "https://{{control-plane-url}}/auth/v2/sessions", 200),
        makeReq("Terminate Session by ID", "DELETE", "https://{{control-plane-url}}/auth/v2/sessions/sess-001", 204)
      ]
    },
    {
      name: "Catalogue & Schema Registry",
      item: [
        makeReq("List Catalogue Resource Items", "GET", "https://{{control-plane-url}}/cat/v2/items?limit=20&offset=0", 200),
        makeReq("Get Catalogue Item by ID", "GET", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01", 200),
        makeReq("Create New Catalogue Resource Item", "POST", "https://{{control-plane-url}}/cat/v2/items", 201, { id: "iudx-res-aqm-01", itemType: "resource", name: "Bangalore Air Quality Sensors", provider: "provider-iudx" }),
        makeReq("Update Catalogue Item Metadata", "PUT", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01", 200, { description: "Updated Real-time sensors in MG Road" }),
        makeReq("Delete Catalogue Resource Item", "DELETE", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01", 204),
        makeReq("Search Catalogue by Full-Text", "GET", "https://{{control-plane-url}}/cat/v2/search?q=environmental+weather", 200),
        makeReq("Search Catalogue by Geo-BoundingBox", "GET", "https://{{control-plane-url}}/cat/v2/search?bbox=77.5,12.9,77.7,13.1", 200),
        makeReq("Search Catalogue by Provider ID", "GET", "https://{{control-plane-url}}/cat/v2/items?provider=provider-iudx", 200),
        makeReq("Search Catalogue by Resource Group", "GET", "https://{{control-plane-url}}/cat/v2/items?resourceGroup=aqm-sensors-group", 200),
        makeReq("List All Registered Schemas", "GET", "https://{{control-plane-url}}/schema/v2/schemas", 200),
        makeReq("Get JSON-LD Schema Definition", "GET", "https://{{control-plane-url}}/schema/v2/schemas/AirQualityObserved", 200),
        makeReq("Register New Data Model Schema", "POST", "https://{{control-plane-url}}/schema/v2/schemas", 201, { schemaName: "WaterQualityObserved", contextUrl: "https://schema.iudx.io/water.jsonld" }),
        makeReq("Validate Payload against Schema", "POST", "https://{{control-plane-url}}/schema/v2/validate", 200, { schema: "AirQualityObserved", data: { pm25: 45, temperature: 28 } }),
        makeReq("Deprecate Schema Version", "POST", "https://{{control-plane-url}}/schema/v2/schemas/AirQualityObserved/deprecate", 200),
        makeReq("List Schema Vocabularies", "GET", "https://{{control-plane-url}}/schema/v2/vocabularies", 200),
        makeReq("Get Vocabulary by Term", "GET", "https://{{control-plane-url}}/schema/v2/vocabularies/co2", 200),
        makeReq("400 Schema Validation Failed", "POST", "https://{{control-plane-url}}/schema/v2/validate", 400, { schema: "AirQualityObserved", data: { invalidField: true } }),
        makeReq("404 Schema Not Found", "GET", "https://{{control-plane-url}}/schema/v2/schemas/UnknownModel", 404),
        makeReq("404 Catalogue Item Not Found", "GET", "https://{{control-plane-url}}/cat/v2/items/non-existent-item", 404),
        makeReq("List Catalogue Item Ratings", "GET", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01/ratings", 200),
        makeReq("Submit Catalogue Item Rating", "POST", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01/ratings", 201, { rating: 5, review: "High fidelity sensor stream" }),
        makeReq("Get Catalogue Item Relationships", "GET", "https://{{control-plane-url}}/cat/v2/items/iudx-res-aqm-01/relationships", 200)
      ]
    },
    {
      name: "Consent & Policy Governance",
      item: [
        makeReq("List Data Access Requests", "GET", "https://{{control-plane-url}}/governance/v2/consent/requests", 200),
        makeReq("Submit Data Access Request", "POST", "https://{{control-plane-url}}/governance/v2/consent/requests", 201, { resourceId: "iudx-res-aqm-01", purpose: "Smart City Analytics", durationDays: 30 }),
        makeReq("Get Access Request by ID", "GET", "https://{{control-plane-url}}/governance/v2/consent/requests/req-001", 200),
        makeReq("Approve Access Request", "POST", "https://{{control-plane-url}}/governance/v2/consent/requests/req-001/approve", 200),
        makeReq("Reject Access Request", "POST", "https://{{control-plane-url}}/governance/v2/consent/requests/req-001/reject", 200, { reason: "Insufficient authorization documents" }),
        makeReq("Revoke Active Consent Grant", "POST", "https://{{control-plane-url}}/governance/v2/consent/grants/grnt-001/revoke", 200),
        makeReq("List Active Access Policies", "GET", "https://{{control-plane-url}}/governance/v2/policies", 200),
        makeReq("Create Resource Access Policy", "POST", "https://{{control-plane-url}}/governance/v2/policies", 201, { resourceId: "iudx-res-aqm-01", rules: [{ role: "consumer", access: "READ_ONLY", rateLimit: 100 }] }),
        makeReq("Update Access Policy Rules", "PUT", "https://{{control-plane-url}}/governance/v2/policies/pol-001", 200, { rules: [{ role: "consumer", access: "READ_ONLY", rateLimit: 500 }] }),
        makeReq("Delete Access Policy", "DELETE", "https://{{control-plane-url}}/governance/v2/policies/pol-001", 204),
        makeReq("Evaluate Policy Permission", "POST", "https://{{control-plane-url}}/governance/v2/policies/evaluate", 200, { consumerId: "usr-001", resourceId: "iudx-res-aqm-01", action: "READ" }),
        makeReq("List Governance Audit Events", "GET", "https://{{control-plane-url}}/governance/v2/audit/events?limit=50", 200),
        makeReq("Filter Audit Events by Resource", "GET", "https://{{control-plane-url}}/governance/v2/audit/events?resourceId=iudx-res-aqm-01", 200),
        makeReq("Filter Audit Events by User", "GET", "https://{{control-plane-url}}/governance/v2/audit/events?userId=usr-001", 200),
        makeReq("Export Governance Compliance Report", "GET", "https://{{control-plane-url}}/governance/v2/audit/compliance-export?format=pdf", 200),
        makeReq("400 Invalid Consent Request Payload", "POST", "https://{{control-plane-url}}/governance/v2/consent/requests", 400, {}),
        makeReq("401 Governance Unauthorized", "GET", "https://{{control-plane-url}}/governance/v2/policies", 401),
        makeReq("404 Policy Not Found", "GET", "https://{{control-plane-url}}/governance/v2/policies/pol-nonexistent", 404),
        makeReq("Create Delegation Token", "POST", "https://{{control-plane-url}}/governance/v2/delegation/token", 201, { delegateTo: "usr-partner-02", resourceId: "iudx-res-aqm-01" }),
        makeReq("Revoke Delegation Token", "DELETE", "https://{{control-plane-url}}/governance/v2/delegation/token/del-001", 204)
      ]
    },
    {
      name: "Provider & Consumer Management",
      item: [
        makeReq("List Data Providers", "GET", "https://{{control-plane-url}}/org/v2/providers", 200),
        makeReq("Get Provider Profile", "GET", "https://{{control-plane-url}}/org/v2/providers/provider-iudx", 200),
        makeReq("Register New Provider Entity", "POST", "https://{{control-plane-url}}/org/v2/providers", 201, { orgName: "Bangalore Smart City Ltd", website: "https://smartcity.blr.gov.in" }),
        makeReq("Update Provider Information", "PATCH", "https://{{control-plane-url}}/org/v2/providers/provider-iudx", 200, { contactEmail: "ops@blr.gov.in" }),
        makeReq("List Data Consumers", "GET", "https://{{control-plane-url}}/org/v2/consumers", 200),
        makeReq("Get Consumer Organization Details", "GET", "https://{{control-plane-url}}/org/v2/consumers/consumer-sirpi", 200),
        makeReq("Register New Consumer Entity", "POST", "https://{{control-plane-url}}/org/v2/consumers", 201, { orgName: "SIRPI Data Research", email: "info@sirpi.io" }),
        makeReq("Get Organization Quota Limits", "GET", "https://{{control-plane-url}}/org/v2/quota/consumer-sirpi", 200),
        makeReq("Update Quota Allocation", "PUT", "https://{{control-plane-url}}/org/v2/quota/consumer-sirpi", 200, { maxRequestsPerDay: 100000, bandwidthGb: 50 }),
        makeReq("Get Organization API Usage Metrics", "GET", "https://{{control-plane-url}}/org/v2/metrics/consumer-sirpi?period=30d", 200),
        makeReq("List Organization Sub-Accounts", "GET", "https://{{control-plane-url}}/org/v2/consumers/consumer-sirpi/subaccounts", 200),
        makeReq("Create Sub-Account", "POST", "https://{{control-plane-url}}/org/v2/consumers/consumer-sirpi/subaccounts", 201, { accountName: "Dev-Team-01" }),
        makeReq("Deactivate Sub-Account", "POST", "https://{{control-plane-url}}/org/v2/consumers/consumer-sirpi/subaccounts/sub-001/deactivate", 200),
        makeReq("Get Current Billing Summary", "GET", "https://{{control-plane-url}}/org/v2/billing/consumer-sirpi/current", 200),
        makeReq("List Past Invoices", "GET", "https://{{control-plane-url}}/org/v2/billing/consumer-sirpi/invoices", 200),
        makeReq("Download Invoice PDF", "GET", "https://{{control-plane-url}}/org/v2/billing/invoices/inv-2026-06/pdf", 200),
        makeReq("401 Provider Management Unauth", "GET", "https://{{control-plane-url}}/org/v2/providers", 401),
        makeReq("404 Provider Not Found", "GET", "https://{{control-plane-url}}/org/v2/providers/provider-nonexistent", 404),
        makeReq("Get Service Health & SLO Metrics", "GET", "https://{{control-plane-url}}/org/v2/slo/status", 200)
      ]
    }
  ]
};

// 3. Files Connect API
const filesConnect = {
  info: {
    _postman_id: "files-connect-001",
    name: "Files Connect API",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Authentication & Secure Tokens",
      item: [
        makeReq("Issue File Server Access Token", "POST", "https://{{files-url}}/file/v1/auth/token", 200, { resourceId: "file-bucket-01", permissions: ["READ", "WRITE"] }),
        makeReq("Verify File Token Validity", "POST", "https://{{files-url}}/file/v1/auth/verify", 200, { token: "{{consumerToken}}" }),
        makeReq("Refresh File Server Token", "POST", "https://{{files-url}}/file/v1/auth/refresh", 200),
        makeReq("Revoke File Server Token", "POST", "https://{{files-url}}/file/v1/auth/revoke", 200, { token: "{{consumerToken}}" }),
        makeReq("401 File Auth Invalid Secret", "POST", "https://{{files-url}}/file/v1/auth/token", 401),
        makeReq("400 Bad Token Scope Request", "POST", "https://{{files-url}}/file/v1/auth/token", 400, { invalidScope: true }),
        makeReq("Get Token Scopes and Perms", "GET", "https://{{files-url}}/file/v1/auth/scopes", 200),
        makeReq("List Active Storage Sessions", "GET", "https://{{files-url}}/file/v1/auth/sessions", 200),
        makeReq("Terminate Storage Session", "DELETE", "https://{{files-url}}/file/v1/auth/sessions/sess-file-01", 204),
        makeReq("Inspect Storage Principal", "GET", "https://{{files-url}}/file/v1/auth/whoami", 200),
        makeReq("403 Token Scope Denied Bucket", "GET", "https://{{files-url}}/file/v1/databank/restricted-bucket/files", 403),
        makeReq("Generate Temporary Upload Secret", "POST", "https://{{files-url}}/file/v1/auth/temp-secret", 201, { validMinutes: 15 })
      ]
    },
    {
      name: "Databank Storage",
      item: [
        makeReq("List Storage Buckets", "GET", "https://{{files-url}}/file/v1/databank/buckets", 200),
        makeReq("Create Storage Bucket", "POST", "https://{{files-url}}/file/v1/databank/buckets", 201, { bucketName: "satellite-imagery-pune", encryption: "AES256" }),
        makeReq("Get Bucket Details & Stats", "GET", "https://{{files-url}}/file/v1/databank/buckets/satellite-imagery-pune", 200),
        makeReq("Delete Storage Bucket", "DELETE", "https://{{files-url}}/file/v1/databank/buckets/satellite-imagery-pune", 204),
        makeReq("List Files in Bucket", "GET", "https://{{files-url}}/file/v1/databank/satellite-imagery-pune/files?limit=50", 200),
        makeReq("Get File Metadata by ID", "GET", "https://{{files-url}}/file/v1/databank/files/f-meta-001", 200),
        makeReq("Download Binary File Stream", "GET", "https://{{files-url}}/file/v1/databank/download/f-meta-001", 200),
        makeReq("Direct Single-part File Upload", "POST", "https://{{files-url}}/file/v1/databank/upload/direct", 201, { fileName: "air-sensor-logs-2026.csv", contentType: "text/csv" }),
        makeReq("Update File Metadata Tags", "PATCH", "https://{{files-url}}/file/v1/databank/files/f-meta-001/tags", 200, { tags: ["air-quality", "pune", "2026"] }),
        makeReq("Delete File from Bucket", "DELETE", "https://{{files-url}}/file/v1/databank/files/f-meta-001", 204),
        makeReq("Copy File across Buckets", "POST", "https://{{files-url}}/file/v1/databank/files/f-meta-001/copy", 200, { destinationBucket: "archive-2026" }),
        makeReq("Move File across Folders", "POST", "https://{{files-url}}/file/v1/databank/files/f-meta-001/move", 200, { destinationPath: "/processed/sensors/" }),
        makeReq("Get File SHA-256 Checksum", "GET", "https://{{files-url}}/file/v1/databank/files/f-meta-001/checksum", 200),
        makeReq("Set File Expiration Lifecycle", "PUT", "https://{{files-url}}/file/v1/databank/files/f-meta-001/lifecycle", 200, { expireAfterDays: 365 }),
        makeReq("List Deleted Files in Recycle Bin", "GET", "https://{{files-url}}/file/v1/databank/recycle-bin", 200),
        makeReq("Restore File from Recycle Bin", "POST", "https://{{files-url}}/file/v1/databank/recycle-bin/f-meta-001/restore", 200),
        makeReq("Purge File Permanently", "DELETE", "https://{{files-url}}/file/v1/databank/recycle-bin/f-meta-001/purge", 204),
        makeReq("404 File Not Found in Bucket", "GET", "https://{{files-url}}/file/v1/databank/files/non-existent-file", 404),
        makeReq("400 Invalid Bucket Name Format", "POST", "https://{{files-url}}/file/v1/databank/buckets", 400, { bucketName: "INVALID NAME!!" }),
        makeReq("Get Bucket Storage Usage Quota", "GET", "https://{{files-url}}/file/v1/databank/buckets/satellite-imagery-pune/quota", 200)
      ]
    },
    {
      name: "Multipart Upload Pipeline",
      item: [
        makeReq("Initiate Multipart Upload Session", "POST", "https://{{files-url}}/file/v1/multipart/initiate", 201, { fileName: "high-res-lidar-cloud.las", fileSize: 524288000, partSize: 10485760 }),
        makeReq("Upload Multipart Chunk Part 1", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/1", 200),
        makeReq("Upload Multipart Chunk Part 2", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/2", 200),
        makeReq("Upload Multipart Chunk Part 3", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/3", 200),
        makeReq("Upload Multipart Chunk Part 4", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/4", 200),
        makeReq("Upload Multipart Chunk Part 5", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/5", 200),
        makeReq("List Uploaded Parts in Session", "GET", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/parts", 200),
        makeReq("Complete Multipart Upload Assembly", "POST", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/complete", 200, { parts: [{ partNumber: 1, etag: "etag-01" }, { partNumber: 2, etag: "etag-02" }] }),
        makeReq("Abort Multipart Upload Session", "DELETE", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/abort", 204),
        makeReq("Get Multipart Session Status", "GET", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01", 200),
        makeReq("List All In-Progress Multipart Sessions", "GET", "https://{{files-url}}/file/v1/multipart/sessions", 200),
        makeReq("400 Missing Chunk Part Number", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/part/invalid", 400),
        makeReq("404 Multipart Session Not Found", "GET", "https://{{files-url}}/file/v1/multipart/sessions/sess-nonexistent", 404),
        makeReq("400 Incomplete Parts for Assembly", "POST", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/complete", 400, { parts: [] }),
        makeReq("Verify Assembled Checksum", "POST", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/verify-checksum", 200, { expectedSha256: "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" }),
        makeReq("Resume Interrupted Chunk Upload", "POST", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/resume", 200),
        makeReq("Set Session Auto-Cleanup Timeout", "PUT", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01/timeout", 200, { timeoutHours: 24 }),
        makeReq("401 Multipart Session Unauthorized", "GET", "https://{{files-url}}/file/v1/multipart/sessions/sess-mp-01", 401)
      ]
    },
    {
      name: "Presigned Storage URLs",
      item: [
        makeReq("Generate Presigned GET Download URL", "POST", "https://{{files-url}}/file/v1/presigned/download", 200, { fileId: "f-meta-001", expirySeconds: 3600 }),
        makeReq("Generate Presigned PUT Upload URL", "POST", "https://{{files-url}}/file/v1/presigned/upload", 200, { fileName: "incoming-data.tar.gz", expirySeconds: 1800 }),
        makeReq("Generate Presigned Multipart Part URLs", "POST", "https://{{files-url}}/file/v1/presigned/multipart", 200, { partsCount: 5, expirySeconds: 7200 }),
        makeReq("Validate Presigned URL Signature", "POST", "https://{{files-url}}/file/v1/presigned/validate", 200, { url: "https://files.iudx.io/signed?sig=abc" }),
        makeReq("Batch Generate Presigned Download URLs", "POST", "https://{{files-url}}/file/v1/presigned/batch-download", 200, { fileIds: ["f-meta-001", "f-meta-002"] }),
        makeReq("400 Presigned Expiry Exceeds Max Allowed", "POST", "https://{{files-url}}/file/v1/presigned/download", 400, { expirySeconds: 9999999 }),
        makeReq("404 Presigned URL File Target Not Found", "POST", "https://{{files-url}}/file/v1/presigned/download", 404, { fileId: "f-nonexistent" }),
        makeReq("Revoke Generated Presigned URL", "POST", "https://{{files-url}}/file/v1/presigned/revoke", 200, { signatureId: "sig-001" }),
        makeReq("Get Presigned URL Access Audit Log", "GET", "https://{{files-url}}/file/v1/presigned/audit/sig-001", 200),
        makeReq("Set Presigned IP Whitelist Filter", "POST", "https://{{files-url}}/file/v1/presigned/ip-filter", 200, { allowedIps: ["103.21.244.0/24"] }),
        makeReq("401 Presigned Generator Unauthorized", "POST", "https://{{files-url}}/file/v1/presigned/download", 401),
        makeReq("Get Presigned Link Expiry Metadata", "GET", "https://{{files-url}}/file/v1/presigned/status/sig-001", 200)
      ]
    },
    {
      name: "Static Asset CDN & Caching",
      item: [
        makeReq("Get CDN Edge Distribution Status", "GET", "https://{{files-url}}/file/v1/cdn/status", 200),
        makeReq("Purge Specific File from Edge Cache", "POST", "https://{{files-url}}/file/v1/cdn/purge", 200, { paths: ["/static/logos/iudx.svg", "/styles/theme.css"] }),
        makeReq("Purge Entire Collection Cache Path", "POST", "https://{{files-url}}/file/v1/cdn/purge-all", 200, { bucket: "static-assets" }),
        makeReq("Get CDN Cache Hit/Miss Metrics", "GET", "https://{{files-url}}/file/v1/cdn/metrics?period=24h", 200),
        makeReq("Configure Custom Cache TTL Rules", "PUT", "https://{{files-url}}/file/v1/cdn/rules/ttl", 200, { staticImagesTtlSeconds: 86400, dataJsonTtlSeconds: 300 }),
        makeReq("Invalidate Stale Asset By Version", "POST", "https://{{files-url}}/file/v1/cdn/invalidate-version", 200, { version: "v2.4.1" }),
        makeReq("Get CDN Origin Latency Stats", "GET", "https://{{files-url}}/file/v1/cdn/origin-latency", 200),
        makeReq("Set Edge Gzip/Brotli Compression", "PUT", "https://{{files-url}}/file/v1/cdn/compression", 200, { brotli: true, gzip: true }),
        makeReq("400 Invalid Purge Wildcard Format", "POST", "https://{{files-url}}/file/v1/cdn/purge", 400, { paths: ["***///invalid"] }),
        makeReq("401 CDN Configuration Unauthorized", "POST", "https://{{files-url}}/file/v1/cdn/purge", 401),
        makeReq("List Active CDN PoPs (Points of Presence)", "GET", "https://{{files-url}}/file/v1/cdn/pops", 200),
        makeReq("Get Edge Geographic Traffic Breakdown", "GET", "https://{{files-url}}/file/v1/cdn/geo-traffic", 200),
        makeReq("Configure CDN SSL/TLS Certificate", "PUT", "https://{{files-url}}/file/v1/cdn/tls", 200, { minVersion: "TLSv1.3" })
      ]
    }
  ]
};

// 4. OGC Geospatial Server
const ogcGeospatial = {
  info: {
    _postman_id: "ogc-geo-001",
    name: "OGC Geospatial Server",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Landing & Conformance",
      item: [
        makeReq("OGC API Features Landing Page", "GET", "https://{{ogc-url}}/ogc/v1", 200),
        makeReq("OGC Conformance Declaration", "GET", "https://{{ogc-url}}/ogc/v1/conformance", 200),
        makeReq("OpenAPI 3.0 Document (JSON)", "GET", "https://{{ogc-url}}/ogc/v1/api", 200),
        makeReq("OpenAPI HTML Documentation", "GET", "https://{{ogc-url}}/ogc/v1/api.html", 200),
        makeReq("Get Supported CRS (Coordinate Reference Systems)", "GET", "https://{{ogc-url}}/ogc/v1/crs", 200),
        makeReq("Get OGC Service Capabilities XML", "GET", "https://{{ogc-url}}/ogc/v1/wfs?request=GetCapabilities", 200),
        makeReq("400 Bad Conformance Parameter", "GET", "https://{{ogc-url}}/ogc/v1/conformance?f=invalid", 400),
        makeReq("401 OGC Landing Unauthorized", "GET", "https://{{ogc-url}}/ogc/v1", 401),
        makeReq("Get OGC Health Status", "GET", "https://{{ogc-url}}/ogc/v1/health", 200),
        makeReq("Get OGC Version & Build Info", "GET", "https://{{ogc-url}}/ogc/v1/version", 200)
      ]
    },
    {
      name: "Collections Metadata",
      item: [
        makeReq("List All Geospatial Collections", "GET", "https://{{ogc-url}}/ogc/v1/collections", 200),
        makeReq("Get Pune Road Network Collection", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads", 200),
        makeReq("Get Bangalore Flood Zones Collection", "GET", "https://{{ogc-url}}/ogc/v1/collections/blr_flood_zones", 200),
        makeReq("Get Air Quality Stations Collection", "GET", "https://{{ogc-url}}/ogc/v1/collections/air_quality_stations", 200),
        makeReq("Get Collection Queryables Definition", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/queryables", 200),
        makeReq("Get Collection JSON Schema", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/schema", 200),
        makeReq("Get Collection Spatial Extent BBox", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/extent", 200),
        makeReq("Create New Feature Collection", "POST", "https://{{ogc-url}}/ogc/v1/collections", 201, { id: "metro_transit_routes", title: "Metro Transit Lines", crs: ["http://www.opengis.net/def/crs/OGC/1.3/CRS84"] }),
        makeReq("Update Collection Description", "PATCH", "https://{{ogc-url}}/ogc/v1/collections/metro_transit_routes", 200, { description: "Updated Phase 2 routes" }),
        makeReq("Delete Feature Collection", "DELETE", "https://{{ogc-url}}/ogc/v1/collections/metro_transit_routes", 204),
        makeReq("404 Collection Not Found", "GET", "https://{{ogc-url}}/ogc/v1/collections/nonexistent_col", 404),
        makeReq("400 Bad Collection Create Payload", "POST", "https://{{ogc-url}}/ogc/v1/collections", 400, {}),
        makeReq("List Collection Storage Partitions", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/partitions", 200),
        makeReq("Refresh Collection Spatial Index", "POST", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/reindex", 200)
      ]
    },
    {
      name: "WFS3 Features",
      item: [
        makeReq("Get Features with BBOX Filter", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?bbox=73.75,18.45,73.95,18.60&limit=50", 200),
        makeReq("Get Features with Datetime Filter", "GET", "https://{{ogc-url}}/ogc/v1/collections/air_quality_stations/items?datetime=2026-01-01T00:00:00Z/2026-06-01T00:00:00Z", 200),
        makeReq("Get Features with CQL2 Property Filter", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?filter=lanes>2+AND+surfaceType='asphalt'", 200),
        makeReq("Get Feature by Specific ID", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/feat-rd-101", 200),
        makeReq("Create Feature in Collection", "POST", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items", 201, { type: "Feature", geometry: { type: "LineString", coordinates: [[73.85, 18.52], [73.86, 18.53]] }, properties: { name: "JM Road", lanes: 4 } }),
        makeReq("Update Feature Properties", "PATCH", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/feat-rd-101", 200, { properties: { surfaceCondition: "excellent" } }),
        makeReq("Replace Feature Geometry & Props", "PUT", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/feat-rd-101", 200, { type: "Feature", geometry: { type: "LineString", coordinates: [[73.85, 18.52], [73.87, 18.54]] } }),
        makeReq("Delete Feature by ID", "DELETE", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/feat-rd-101", 204),
        makeReq("400 Invalid BBOX Coordinates Count", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?bbox=73.75,18.45", 400),
        makeReq("404 Feature ID Not Found", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/feat-nonexistent", 404),
        makeReq("401 WFS Features Unauthorized", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items", 401),
        makeReq("Get Features GeoJSON Format", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?f=geojson", 200),
        makeReq("Get Features FlatGeobuf Format", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?f=fgb", 200),
        makeReq("Get Features Pagination with Token", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?token=page-token-2&limit=25", 200),
        makeReq("Bulk Insert GeoJSON FeatureCollection", "POST", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items/bulk", 201, { type: "FeatureCollection", features: [] }),
        makeReq("Count Features Matching Criteria", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/items?count=true", 200)
      ]
    },
    {
      name: "Vector & Raster Tiles",
      item: [
        makeReq("Get TileMatrixSets Metadata", "GET", "https://{{ogc-url}}/ogc/v1/tileMatrixSets", 200),
        makeReq("Get WebMercatorQuad TileMatrixSet", "GET", "https://{{ogc-url}}/ogc/v1/tileMatrixSets/WebMercatorQuad", 200),
        makeReq("Get Collection TileJSON Metadata", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/tiles/WebMercatorQuad/tilejson.json", 200),
        makeReq("Fetch MVT Vector Tile (z=14, x=11640, y=7240)", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/tiles/WebMercatorQuad/14/11640/7240.mvt", 200),
        makeReq("Fetch Raster Map Tile PNG (z=14)", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/tiles/WebMercatorQuad/14/11640/7240.png", 200),
        makeReq("Fetch Mapbox Style Document", "GET", "https://{{ogc-url}}/ogc/v1/styles/dark-modern-v1.json", 200),
        makeReq("Get 3D Terrain Mesh Tile (z=12)", "GET", "https://{{ogc-url}}/ogc/v1/terrain/tiles/12/2910/1810.terrain", 200),
        makeReq("404 Vector Tile Out of Range", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/tiles/WebMercatorQuad/28/9999999/9999999.mvt", 404),
        makeReq("400 Invalid Tile Matrix Identifier", "GET", "https://{{ogc-url}}/ogc/v1/collections/pune_roads/tiles/InvalidMatrix/14/11640/7240.mvt", 400),
        makeReq("Purge Tile Cache by Bounding Box", "POST", "https://{{ogc-url}}/ogc/v1/tiles/cache/purge", 200, { bbox: [73.75, 18.45, 73.95, 18.60] })
      ]
    },
    {
      name: "Spatial Processes & WPS",
      item: [
        makeReq("List Available Spatial Processes", "GET", "https://{{ogc-url}}/ogc/v1/processes", 200),
        makeReq("Describe Buffer Process", "GET", "https://{{ogc-url}}/ogc/v1/processes/buffer", 200),
        makeReq("Describe Spatial Intersection Process", "GET", "https://{{ogc-url}}/ogc/v1/processes/intersection", 200),
        makeReq("Describe Convex Hull Process", "GET", "https://{{ogc-url}}/ogc/v1/processes/convexhull", 200),
        makeReq("Execute Buffer Process Job", "POST", "https://{{ogc-url}}/ogc/v1/processes/buffer/execution", 201, { inputs: { featureId: "feat-rd-101", distanceMeters: 250 } }),
        makeReq("Execute Spatial Intersection Job", "POST", "https://{{ogc-url}}/ogc/v1/processes/intersection/execution", 201, { inputs: { layerA: "pune_roads", layerB: "blr_flood_zones" } }),
        makeReq("Get Process Execution Job Status", "GET", "https://{{ogc-url}}/ogc/v1/jobs/job-spatial-01", 200),
        makeReq("Get Completed Process Results GeoJSON", "GET", "https://{{ogc-url}}/ogc/v1/jobs/job-spatial-01/results", 200),
        makeReq("Cancel In-Progress Spatial Job", "DELETE", "https://{{ogc-url}}/ogc/v1/jobs/job-spatial-01", 204),
        makeReq("404 Spatial Job Not Found", "GET", "https://{{ogc-url}}/ogc/v1/jobs/job-nonexistent", 404)
      ]
    }
  ]
};

// 5. Sandbox AI ML
const sandboxAi = {
  info: {
    _postman_id: "sandbox-ai-001",
    name: "Sandbox AI ML",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Model Serving & Inference",
      item: [
        makeReq("Predict Traffic Congestion", "POST", "https://{{sandbox-url}}/models/v1/traffic-forecast/predict", 200, { features: { hour: 17, dayOfWeek: 5, rainfallMm: 12.5, currentSpeedAvg: 18.2 } }),
        makeReq("Predict Air Quality Index (AQI 24h)", "POST", "https://{{sandbox-url}}/models/v1/aqi-predictor/predict", 200, { features: { pm25_lag1: 65, pm10_lag1: 120, windSpeed: 4.2 } }),
        makeReq("Explain Inference with SHAP Values", "POST", "https://{{sandbox-url}}/models/v1/aqi-predictor/explain", 200, { instance: { pm25_lag1: 65, pm10_lag1: 120 } }),
        makeReq("Submit Batch Inference Job", "POST", "https://{{sandbox-url}}/models/v1/batch-predict", 202, { modelName: "aqi-predictor", inputDatasetUrl: "s3://sandbox/input-batch.parquet" }),
        makeReq("Get Batch Inference Status", "GET", "https://{{sandbox-url}}/models/v1/batch-predict/job-inf-01", 200),
        makeReq("Download Batch Prediction Output", "GET", "https://{{sandbox-url}}/models/v1/batch-predict/job-inf-01/results", 200),
        makeReq("Model Health & Liveness Probe", "GET", "https://{{sandbox-url}}/models/v1/aqi-predictor/health", 200),
        makeReq("Model Metadata & Input Schema", "GET", "https://{{sandbox-url}}/models/v1/aqi-predictor/metadata", 200),
        makeReq("Warm-up Model GPU Cache", "POST", "https://{{sandbox-url}}/models/v1/aqi-predictor/warmup", 200),
        makeReq("400 Bad Inference Feature Vector", "POST", "https://{{sandbox-url}}/models/v1/traffic-forecast/predict", 400, { features: { missingAllFields: true } }),
        makeReq("404 Model Target Not Found", "POST", "https://{{sandbox-url}}/models/v1/unknown-model/predict", 404),
        makeReq("401 Model Inference Unauthorized", "POST", "https://{{sandbox-url}}/models/v1/traffic-forecast/predict", 401),
        makeReq("Get Real-time Latency Metrics", "GET", "https://{{sandbox-url}}/models/v1/aqi-predictor/metrics", 200),
        makeReq("Evaluate Model Drift Score", "GET", "https://{{sandbox-url}}/models/v1/aqi-predictor/drift-report", 200)
      ]
    },
    {
      name: "Model Registry & Versions",
      item: [
        makeReq("List Registered AI Models", "GET", "https://{{sandbox-url}}/registry/v1/models", 200),
        makeReq("Register New Model Asset", "POST", "https://{{sandbox-url}}/registry/v1/models", 201, { name: "water-anomaly-detector", framework: "pytorch", version: "1.0.0" }),
        makeReq("Get Model Version Details", "GET", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/1.0.0", 200),
        makeReq("Promote Model to Production Stage", "POST", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/1.0.0/transition", 200, { stage: "Production" }),
        makeReq("Rollback Model to Previous Version", "POST", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/rollback", 200, { targetVersion: "0.9.4" }),
        makeReq("List Model Tags and Hyperparams", "GET", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/tags", 200),
        makeReq("Set Model Performance Benchmark", "PUT", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/benchmarks", 200, { f1Score: 0.942, latencyMs: 14.5 }),
        makeReq("Deprecate Model Asset", "DELETE", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/0.8.0", 204),
        makeReq("404 Model Version Not Found", "GET", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/99.99.99", 404),
        makeReq("400 Invalid Stage Name Transition", "POST", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/1.0.0/transition", 400, { stage: "INVALID_STAGE" }),
        makeReq("Download Model Artifact Binary", "GET", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/versions/1.0.0/download", 200),
        makeReq("Compare Model Versions Side-by-Side", "GET", "https://{{sandbox-url}}/registry/v1/models/water-anomaly-detector/compare?v1=1.0.0&v2=0.9.4", 200)
      ]
    },
    {
      name: "Synthetic Data Generation",
      item: [
        makeReq("Submit Tabular Synthetic Generation Job", "POST", "https://{{sandbox-url}}/synthetic/v1/generate/tabular", 202, { sourceDatasetId: "iudx-res-aqm-01", sampleCount: 50000, privacyEpsilon: 1.2 }),
        makeReq("Submit Time-series Synthesis Job", "POST", "https://{{sandbox-url}}/synthetic/v1/generate/timeseries", 202, { sourceDatasetId: "traffic-sensors-blr", durationDays: 180, seasonality: "weekly" }),
        makeReq("Submit Spatial Trajectory Synthesis", "POST", "https://{{sandbox-url}}/synthetic/v1/generate/trajectories", 202, { bbox: [77.5, 12.9, 77.7, 13.1], vehicleCount: 500 }),
        makeReq("Get Synthesis Job Execution Status", "GET", "https://{{sandbox-url}}/synthetic/v1/jobs/job-syn-01", 200),
        makeReq("Download Synthetic Data (Parquet)", "GET", "https://{{sandbox-url}}/synthetic/v1/jobs/job-syn-01/download?format=parquet", 200),
        makeReq("Download Synthetic Data (CSV)", "GET", "https://{{sandbox-url}}/synthetic/v1/jobs/job-syn-01/download?format=csv", 200),
        makeReq("Get Differential Privacy Quality Score", "GET", "https://{{sandbox-url}}/synthetic/v1/jobs/job-syn-01/privacy-report", 200),
        makeReq("Cancel Running Synthesis Job", "DELETE", "https://{{sandbox-url}}/synthetic/v1/jobs/job-syn-01", 204),
        makeReq("400 Bad Synthetic Parameter Value", "POST", "https://{{sandbox-url}}/synthetic/v1/generate/tabular", 400, { sampleCount: -500 }),
        makeReq("404 Synthesis Job Not Found", "GET", "https://{{sandbox-url}}/synthetic/v1/jobs/job-nonexistent", 404)
      ]
    },
    {
      name: "Feature Store & MLflow",
      item: [
        makeReq("List Feature Store Groups", "GET", "https://{{sandbox-url}}/features/v1/groups", 200),
        makeReq("Get Online Features by Entity ID", "POST", "https://{{sandbox-url}}/features/v1/online-features", 200, { entityId: "urn:ngsi-ld:Device:001", featureNames: ["avg_speed_1h", "temp_ema_24h"] }),
        makeReq("Register New Feature Group", "POST", "https://{{sandbox-url}}/features/v1/groups", 201, { name: "smart_meter_features", primaryKey: "meter_id", timestampKey: "read_time" }),
        makeReq("Submit Offline Batch Feature Extract", "POST", "https://{{sandbox-url}}/features/v1/batch-extract", 202, { group: "smart_meter_features", fromDate: "2026-01-01", toDate: "2026-06-01" }),
        makeReq("List MLflow Active Experiments", "GET", "https://{{sandbox-url}}/mlflow/v1/experiments", 200),
        makeReq("Log Experiment Run Metrics", "POST", "https://{{sandbox-url}}/mlflow/v1/runs/log-metrics", 200, { runId: "run-001", metrics: { loss: 0.042, val_accuracy: 0.961 } }),
        makeReq("Get Experiment Best Run Summary", "GET", "https://{{sandbox-url}}/mlflow/v1/experiments/exp-01/best-run", 200),
        makeReq("404 Feature Group Not Found", "GET", "https://{{sandbox-url}}/features/v1/groups/nonexistent_group", 404),
        makeReq("401 Feature Store Unauthorized", "GET", "https://{{sandbox-url}}/features/v1/groups", 401)
      ]
    }
  ]
};

// 6. Async Ingestion & RMQ Broker
const asyncIngestion = {
  info: {
    _postman_id: "async-ingest-001",
    name: "Async Ingestion & RMQ Broker",
    schema: "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"
  },
  item: [
    {
      name: "Message Ingestion API",
      item: [
        makeReq("Publish Single Entity Message", "POST", "https://{{ingest-url}}/ingest/v1/publish", 200, { routingKey: "iudx.environment.aqm.blr", message: { entityId: "urn:ngsi-ld:AQM:001", pm25: 42.1, time: "2026-06-09T10:00:00Z" } }),
        makeReq("Bulk Ingest Batch Messages", "POST", "https://{{ingest-url}}/ingest/v1/bulk-publish", 200, { messages: [{ routingKey: "iudx.environment.aqm.blr", message: { id: "01" } }, { routingKey: "iudx.transport.speed.blr", message: { id: "02" } }] }),
        makeReq("Ingestion Health & Backpressure Probe", "GET", "https://{{ingest-url}}/ingest/v1/health", 200),
        makeReq("Validate Ingestion Message Schema", "POST", "https://{{ingest-url}}/ingest/v1/validate-message", 200, { schema: "AQM-Telemetry", payload: { pm25: 35 } }),
        makeReq("Get Ingestion Ingress Rate (msg/sec)", "GET", "https://{{ingest-url}}/ingest/v1/metrics/ingress-rate", 200),
        makeReq("400 Bad Ingestion Payload Format", "POST", "https://{{ingest-url}}/ingest/v1/publish", 400, { invalidMessage: null }),
        makeReq("401 Ingestion Token Unauthorized", "POST", "https://{{ingest-url}}/ingest/v1/publish", 401),
        makeReq("404 Ingestion Route Exchange Not Found", "POST", "https://{{ingest-url}}/ingest/v1/publish", 404, { routingKey: "unknown.exchange.route" }),
        makeReq("Pause Ingestion Stream Intake", "POST", "https://{{ingest-url}}/ingest/v1/admin/pause", 200),
        makeReq("Resume Ingestion Stream Intake", "POST", "https://{{ingest-url}}/ingest/v1/admin/resume", 200),
        makeReq("Get Ingestion Buffer Utilization", "GET", "https://{{ingest-url}}/ingest/v1/metrics/buffer", 200),
        makeReq("Flush Ingestion In-Flight Buffer", "POST", "https://{{ingest-url}}/ingest/v1/admin/flush", 200)
      ]
    },
    {
      name: "Exchange & Queue Management",
      item: [
        makeReq("List RabbitMQ Exchanges", "GET", "https://{{broker-url}}/broker/v1/exchanges", 200),
        makeReq("Declare Topic Exchange", "POST", "https://{{broker-url}}/broker/v1/exchanges", 201, { name: "iudx.topic.telemetry", type: "topic", durable: true }),
        makeReq("List Active Broker Queues", "GET", "https://{{broker-url}}/broker/v1/queues", 200),
        makeReq("Declare Consumer Ingestion Queue", "POST", "https://{{broker-url}}/broker/v1/queues", 201, { name: "q.consumer.analytics.blr", durable: true, autoDelete: false }),
        makeReq("Bind Queue to Topic Exchange", "POST", "https://{{broker-url}}/broker/v1/bindings", 201, { exchange: "iudx.topic.telemetry", queue: "q.consumer.analytics.blr", routingKey: "iudx.environment.*" }),
        makeReq("Unbind Queue from Exchange", "DELETE", "https://{{broker-url}}/broker/v1/bindings", 204, { exchange: "iudx.topic.telemetry", queue: "q.consumer.analytics.blr", routingKey: "iudx.environment.*" }),
        makeReq("Inspect Queue Depth and Unacked Count", "GET", "https://{{broker-url}}/broker/v1/queues/q.consumer.analytics.blr", 200),
        makeReq("Purge All Messages in Queue", "DELETE", "https://{{broker-url}}/broker/v1/queues/q.consumer.analytics.blr/contents", 204),
        makeReq("Declare Dead-Letter Queue (DLQ)", "POST", "https://{{broker-url}}/broker/v1/queues/dlq", 201, { name: "q.dlq.telemetry.failed", ttlMs: 604800000 }),
        makeReq("Replay Dead-Letter Queue Messages", "POST", "https://{{broker-url}}/broker/v1/queues/dlq/replay", 200, { targetQueue: "q.consumer.analytics.blr" }),
        makeReq("404 Exchange Not Found", "GET", "https://{{broker-url}}/broker/v1/exchanges/nonexistent", 404),
        makeReq("401 Broker Admin Unauthorized", "GET", "https://{{broker-url}}/broker/v1/queues", 401)
      ]
    },
    {
      name: "Streaming & Subscriptions",
      item: [
        makeReq("Connect WebSocket Live Feed Stream", "GET", "https://{{broker-url}}/broker/v1/ws/subscribe?topic=iudx.environment.aqm", 101),
        makeReq("Subscribe Server-Sent Events (SSE) Stream", "GET", "https://{{broker-url}}/broker/v1/sse/subscribe?topic=iudx.transport", 200),
        makeReq("List Active Live Streaming Clients", "GET", "https://{{broker-url}}/broker/v1/streams/clients", 200),
        makeReq("Get Streaming Latency Performance", "GET", "https://{{broker-url}}/broker/v1/streams/metrics/latency", 200),
        makeReq("Disconnect Streaming Client Session", "DELETE", "https://{{broker-url}}/broker/v1/streams/clients/client-ws-01", 204),
        makeReq("Set Stream Rate-Limiting Cap", "PUT", "https://{{broker-url}}/broker/v1/streams/rate-limit", 200, { maxMsgsPerSecond: 500 }),
        makeReq("Get Streaming Consumer Lag", "GET", "https://{{broker-url}}/broker/v1/streams/consumer-lag", 200),
        makeReq("400 Invalid SSE Topic Filter", "GET", "https://{{broker-url}}/broker/v1/sse/subscribe?topic=***", 400),
        makeReq("401 Streaming Token Unauthorized", "GET", "https://{{broker-url}}/broker/v1/sse/subscribe", 401),
        makeReq("MQTT-to-HTTP Bridge Health", "GET", "https://{{broker-url}}/broker/v1/mqtt-bridge/health", 200),
        makeReq("Stream Cluster Replication Status", "GET", "https://{{broker-url}}/broker/v1/cluster/status", 200)
      ]
    }
  ]
};

const collections = [
  { file: "DX-Data-Plane SIRPI.postman_collection (3).json", data: dxDataPlane },
  { file: "Control Plane IUDX.postman_collection.json", data: controlPlane },
  { file: "Files Connect API.postman_collection.json", data: filesConnect },
  { file: "OGC Geospatial Server.postman_collection.json", data: ogcGeospatial },
  { file: "Sandbox AI ML.postman_collection.json", data: sandboxAi },
  { file: "Async Ingestion & RMQ Broker.postman_collection.json", data: asyncIngestion }
];

function countItems(items) {
  let count = 0;
  for (const it of items || []) {
    if (it.item) count += countItems(it.item);
    else count++;
  }
  return count;
}

let grandTotal = 0;
for (const col of collections) {
  const filePath = path.join(collectionsDir, col.file);
  fs.writeFileSync(filePath, JSON.stringify(col.data, null, 2), "utf8");
  const count = countItems(col.data.item);
  grandTotal += count;
  console.log(`Wrote ${col.file}: ${count} APIs`);
}

console.log(`\nGRAND TOTAL APIS GENERATED: ${grandTotal}`);
