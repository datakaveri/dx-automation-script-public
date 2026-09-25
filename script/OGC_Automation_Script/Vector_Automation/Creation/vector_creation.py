import asyncio
import json
import logging
from datetime import datetime
import aiohttp


class GPKGOnboarder:

  def __init__(self, config_path="config.json"):
    with open(config_path, "r", encoding="utf-8") as f:
      self.config = json.load(f)

    self.base_url = self.config["base_url"].rstrip("/")
    self.processes_url = f"{self.base_url}/processes"
    self.auth_header = {
        "Authorization": f"Bearer {self.config['token']}",
        "Content-Type": "application/json",
    }
    self.files = self.config.get("files", [])
    self.bucket_name = self.config["bucket_name"]
    self.region = self.config["region"]
    self.presigned_url_process_id = self.config["presigned_url_process_id"]
    self.collection_onboarding_process_id = self.config[
        "collection_onboarding_process_id"
    ]
    self.batch_size = self.config.get("batch_size", 5)

    self.logger = self.setup_logger()

  def setup_logger(self):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_filename = f"gpkg_onboarding_status_{timestamp}.log"
    logger = logging.getLogger("gpkg_onboarding_status_logger")
    logger.setLevel(logging.INFO)
    file_handler = logging.FileHandler(log_filename)
    formatter = logging.Formatter("%(asctime)s - %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger

  async def generate_presigned_url(self, session, resource_id, file):
    payload = {
        "inputs": {
            "resourceId": resource_id,
            "itemId": resource_id,
            "bucketName": self.bucket_name,
            "region": self.region,
            "fileType": "GeoPackage",
            "version": "1.0.0",
            "s3BucketIdentifier": "default",
        }
    }
    if "rg-id" in file:
      payload["inputs"]["collectionId"] = file["rg-id"]

    url = f"{self.processes_url}/{self.presigned_url_process_id}/execution"

    async with session.post(
        url, headers=self.auth_header, json=payload, timeout=120
    ) as response:
      try:
        data = await response.json()
      except Exception:
        data = {}

      if response.status not in (200, 201):
        self.logger.error(
            f"Generate Pre-signed URL error [{response.status}]: {data}"
        )
        return None, None

      return data.get("S3PreSignedUrl"), data.get("s3ObjectKeyName")

  async def upload_file(self, session, file_path, presigned_url):
    try:
      with open(file_path, "rb") as f:
        async with session.put(presigned_url, data=f) as response:
          return response.status in (200, 201)
    except Exception as e:
      self.logger.error(f"File upload exception for {file_path}: {e}")
      return False

  async def trigger_collection_onboarding(self, session, file, resource_id):
    folder_prefix = file.get("rg-id", resource_id)
    file_name = (
        f"{folder_prefix}/{resource_id}.gpkg"
        if "rg-id" in file
        else f"{resource_id}.gpkg"
    )

    payload = {
        "inputs": {
            "fileName": file_name,
            "title": file.get("label", ""),
            "description": file.get("description", ""),
            "resourceId": resource_id,
            "version": "1.0.0",
            "s3BucketIdentifier": "default",
        },
        "response": "raw",
    }

    url = (
        f"{self.processes_url}/{self.collection_onboarding_process_id}/execution"
    )

    async with session.post(
        url, headers=self.auth_header, json=payload, timeout=120
    ) as response:
      try:
        data = await response.json()
      except Exception:
        data = {}

      if response.status not in (200, 201):
        self.logger.error(
            f"Trigger Onboarding error [{response.status}]: {data}"
        )
        return None

      return data.get("jobId")

  async def check_job_status(self, session, job_id):
    job_url = f"{self.base_url}/jobs/{job_id}"
    while True:
      async with session.get(job_url, headers=self.auth_header) as response:
        if response.status != 200:
          self.logger.warning(
              f"Job status check returned HTTP {response.status}. Retrying..."
          )
          await asyncio.sleep(2)
          continue

        data = await response.json()
        status = data.get("status")

        if status is None:
          self.logger.warning(
              f"No 'status' field for job_id: {job_id}. Retrying..."
          )
          await asyncio.sleep(2)
          continue

        retrieved_job_id = data.get("id", job_id)
        print(f"Job {retrieved_job_id} Status: {status}")

        if status in ["SUCCESSFUL", "FAILED"]:
          return status, retrieved_job_id

      await asyncio.sleep(5)

  async def process_gpkg_file(self, session, file):
    label = file.get("label", "Unknown Label")
    resource_id = file["ri_uuid"]
    print(f"\n[Processing] File: {file['file_path']} | UUID: {resource_id}")

    # 1. Generate Presigned URL
    presigned_url, s3_object_key = await self.generate_presigned_url(
        session, resource_id, file
    )
    if not presigned_url:
      print(f"Failed to get pre-signed URL for {label}")
      return

    # 2. Upload file to S3
    await asyncio.sleep(1)
    uploaded = await self.upload_file(
        session, file["file_path"], presigned_url
    )
    if not uploaded:
      print(f"Upload failed for {label}")
      return

    print(f"Upload successful for {label}")

    # 3. Trigger Collection Onboarding Process
    await asyncio.sleep(2)
    job_id = await self.trigger_collection_onboarding(
        session, file, resource_id
    )
    if not job_id:
      print(f"Failed to trigger onboarding for {label}")
      return

    # 4. Check Job Status
    await asyncio.sleep(2)
    status, final_job_id = await self.check_job_status(session, job_id)
    print(f"Onboarding Status for {label}: {status}")
    self.logger.info(
        f"Onboarding Status for {label} (Job ID: {final_job_id}): {status}"
    )

  async def run(self):
    async with aiohttp.ClientSession() as session:
      total_files = len(self.files)
      for i in range(0, total_files, self.batch_size):
        batch = self.files[i : i + self.batch_size]
        self.logger.info(
            f"Processing batch {i // self.batch_size + 1}: {len(batch)}"
            " file(s)"
        )
        await asyncio.gather(
            *(self.process_gpkg_file(session, file) for file in batch)
        )
        self.logger.info(f"Finished batch {i // self.batch_size + 1}")


if __name__ == "__main__":
  onboarder = GPKGOnboarder("config.json")
  asyncio.run(onboarder.run())