import os
import sys
import json
import time
import threading
from pathlib import Path

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

# =====================================================================
# Load Configuration from JSON File
# =====================================================================

CONFIG_FILE = "config.json"

def load_config(config_path: str = CONFIG_FILE) -> dict:
    """Load configuration from a JSON file."""
    path = Path(config_path)
    if not path.is_file():
        print(f"❌ Configuration file '{config_path}' not found.")
        sys.exit(1)
        
    with open(path, "r") as f:
        return json.load(f)

config = load_config()

S3_BUCKET = config.get("bucket")
S3_ENDPOINT = config.get("endpoint")
S3_REGION = config.get("region", "ap-south-1")
S3_ACCESS_KEY = config.get("aws_access_key_id")
S3_SECRET_KEY = config.get("aws_secret_access_key")
DIRECTORY = config.get("directory")

if not S3_BUCKET:
    print("❌ 'bucket' name is missing in config.json")
    sys.exit(1)

if not DIRECTORY:
    print("❌ 'directory' path is missing in config.json")
    sys.exit(1)

# =====================================================================
# Boto3 Client Setup
# =====================================================================

client_config = Config(
    connect_timeout=10,
    read_timeout=120,
    retries={"max_attempts": 3},
    s3={"addressing_style": "path"},
)

# Optional endpoint_url: pass None if empty or standard AWS
endpoint_url = S3_ENDPOINT if S3_ENDPOINT else None

s3 = boto3.client(
    "s3",
    endpoint_url=endpoint_url,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=client_config,
)

transfer_config = TransferConfig(
    multipart_threshold=100 * 1024 * 1024,
    multipart_chunksize=100 * 1024 * 1024,
    max_concurrency=10,
    use_threads=True,
)

# =====================================================================
# Progress Callback
# =====================================================================

class ProgressPercentage:
    def __init__(self, filename):
        self.filename = filename
        self.size = float(os.path.getsize(filename))
        self.seen = 0
        self.start = time.time()
        self.lock = threading.Lock()

    def __call__(self, bytes_amount):
        with self.lock:
            self.seen += bytes_amount

            percent = (self.seen / self.size) * 100
            elapsed = max(time.time() - self.start, 0.001)

            speed = self.seen / 1024 / 1024 / elapsed

            print(
                f"\r   {percent:6.2f}% | "
                f"{self.seen/1024/1024:8.2f}/"
                f"{self.size/1024/1024:8.2f} MB | "
                f"{speed:7.2f} MB/s",
                end="",
                flush=True,
            )

# =====================================================================
# Validate Bucket Access
# =====================================================================

print("=" * 80)
print(f"Testing access to bucket: '{S3_BUCKET}'...")
print("=" * 80)

try:
    s3.head_bucket(Bucket=S3_BUCKET)
    print("✓ Successfully connected to bucket")
except ClientError as e:
    error_code = e.response.get('Error', {}).get('Code')
    if error_code == '403':
        print(f"❌ Access denied to bucket '{S3_BUCKET}'. Check credentials/permissions.")
    elif error_code == '404':
        print(f"❌ Bucket '{S3_BUCKET}' does not exist.")
    else:
        print(f"❌ Connection error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"\n❌ Could not connect to S3: {e}")
    sys.exit(1)

print()

# =====================================================================
# Find Files
# =====================================================================

print("=" * 80)
print("Scanning directory")
print(DIRECTORY)
print("=" * 80)

files = []

for root, dirs, filenames in os.walk(DIRECTORY):
    print(f"Scanning: {root}")

    for f in filenames:
        if f.lower().endswith((".tif", ".tiff", ".aux.xml")):
            files.append(os.path.join(root, f))

print(f"\nFound {len(files)} files\n")

if not files:
    print("No files found.")
    sys.exit(0)

# =====================================================================
# Upload
# =====================================================================

uploaded = 0
failed = 0

overall_start = time.time()

for index, local_file in enumerate(files, start=1):

    relative = os.path.relpath(local_file, DIRECTORY)
    folder_name = os.path.basename(DIRECTORY)
    s3_key = f"{folder_name}/{relative.replace(os.sep, '/')}"

    size_mb = os.path.getsize(local_file) / 1024 / 1024

    print("\n" + "=" * 80)
    print(f"[{index}/{len(files)}]")
    print(f"Uploading : {relative}")
    print(f"Local File: {local_file}")
    print(f"S3 Key    : {s3_key}")
    print(f"Size      : {size_mb:.2f} MB")
    print("=" * 80)

    start = time.time()

    try:
        s3.upload_file(
            Filename=local_file,
            Bucket=S3_BUCKET,
            Key=s3_key,
            Config=transfer_config,
            Callback=ProgressPercentage(local_file),
        )

        elapsed = time.time() - start
        avg_speed = size_mb / max(elapsed, 0.001)

        uploaded += 1

        print(
            f"\n✓ Uploaded in {elapsed:.2f} sec "
            f"({avg_speed:.2f} MB/s)"
        )

    except Exception as e:
        failed += 1
        print("\n❌ Upload Failed")
        print(type(e).__name__)
        print(e)

# =====================================================================
# Summary
# =====================================================================

total_time = time.time() - overall_start

print("\n")
print("=" * 80)
print("UPLOAD SUMMARY")
print("=" * 80)
print(f"Total Files : {len(files)}")
print(f"Uploaded    : {uploaded}")
print(f"Failed      : {failed}")
print(f"Elapsed     : {total_time:.2f} sec")
print("=" * 80)
