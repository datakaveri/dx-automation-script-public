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

// Complete Files Connect API matching EXACT screenshot hierarchy
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
        },
        {
          name: "10. Download Databank",
          item: [
            makeReq("Download databank - 200 OK", "GET", "https://{{files-url}}/file/v1/databank/download-all?bucket=databank-01", 200),
            makeReq("Download databank - 404 Not Found", "GET", "https://{{files-url}}/file/v1/databank/download-all?bucket=unknown", 404)
          ]
        },
        {
          name: "11. Download Report PDF",
          item: [
            makeReq("Download report PDF - 200 OK", "GET", "https://{{files-url}}/file/v1/reports/pdf?reportId=rep-001", 200),
            makeReq("Download report PDF - 404 Not Found", "GET", "https://{{files-url}}/file/v1/reports/pdf?reportId=rep-nonexistent", 404)
          ]
        },
        {
          name: "12. Create Process Job",
          item: [
            makeReq("Create process job - 202", "POST", "https://{{files-url}}/file/v1/jobs/process", 202, { operation: "compress", targetFileId: "f-101" }),
            makeReq("Create process job - 400 Bad Request", "POST", "https://{{files-url}}/file/v1/jobs/process", 400, {})
          ]
        },
        {
          name: "13. Get Job Status",
          item: [
            makeReq("Get job status - 200 OK", "GET", "https://{{files-url}}/file/v1/jobs/job-proc-01", 200),
            makeReq("Get job status - 404 Not Found", "GET", "https://{{files-url}}/file/v1/jobs/job-proc-nonexistent", 404)
          ]
        },
        {
          name: "14. Update Job Status",
          item: [
            makeReq("Update job status - 200 OK", "PUT", "https://{{files-url}}/file/v1/jobs/job-proc-01/status", 200, { status: "COMPLETED" }),
            makeReq("Update job status - 404 Not Found", "PUT", "https://{{files-url}}/file/v1/jobs/job-proc-nonexistent/status", 404, { status: "FAILED" })
          ]
        }
      ]
    },
    {
      name: "Assets",
      item: [
        {
          name: "1. Upload Asset",
          item: [
            makeReq("Upload asset - 201", "POST", "https://{{files-url}}/file/v1/assets/upload", 201, { assetName: "CityMapRaster.png", contentType: "image/png" }),
            makeReq("Upload asset - 400", "POST", "https://{{files-url}}/file/v1/assets/upload", 400, {})
          ]
        },
        {
          name: "2. Download Asset",
          item: [
            makeReq("Download asset - 200 OK", "POST", "https://{{files-url}}/file/v1/assets/download", 200, { assetId: "ast-01" }),
            makeReq("Download asset - 404", "POST", "https://{{files-url}}/file/v1/assets/download", 404, { assetId: "ast-nonexistent" })
          ]
        }
      ]
    }
  ]
};

fs.writeFileSync(path.join(collectionsDir, "Files Connect API.postman_collection.json"), JSON.stringify(filesConnect, null, 2), "utf8");
console.log("Updated Files Connect API collection with complete 14 Databanks + 2 Assets folders!");
