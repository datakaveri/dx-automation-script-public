import os
import sys
import json
import logging
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from dataclasses import dataclass
from datetime import datetime

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, PartialCredentialsError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('s3_deletion.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class S3Config:
    """S3 configuration settings."""
    bucket: str
    region: str = 'us-east-1'
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None

    def __post_init__(self):
        """Validate configuration."""
        if not self.bucket:
            raise ValueError("S3 bucket name is required")


class S3Deleter:
    """Handles S3 file/folder deletion with UUID-based lookup."""

    def __init__(self, config: S3Config):
        """
        Initialize S3 deleter.
        
        Args:
            config: S3Configuration object
            
        Raises:
            NoCredentialsError: If AWS credentials are not provided or found
        """
        self.config = config
        self.stats = {
            'gpkg_deleted': 0,
            'tif_deleted': 0,
            'folders_deleted': 0,
            'total_size_deleted': 0,
            'errors': 0
        }

        try:
            self.s3_client = boto3.client(
                's3',
                region_name=config.region,
                aws_access_key_id=config.aws_access_key_id,
                aws_secret_access_key=config.aws_secret_access_key
            )
            # Test credentials
            self.s3_client.head_bucket(Bucket=config.bucket)
            logger.info(f"Successfully connected to bucket: {config.bucket}")
        except (NoCredentialsError, PartialCredentialsError) as e:
            logger.error(f"AWS credentials error: {e}")
            raise
        except ClientError as e:
            logger.error(f"Cannot access bucket '{config.bucket}': {e}")
            raise

    def _format_size(self, bytes_size: int) -> str:
        """Format bytes to human-readable format."""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if bytes_size < 1024:
                return f"{bytes_size:.2f} {unit}"
            bytes_size /= 1024
        return f"{bytes_size:.2f} TB"

    def _list_s3_objects(self, prefix: str = '') -> List[Dict]:
        """
        List all objects in S3 with given prefix.
        
        Args:
            prefix: S3 prefix to search
            
        Returns:
            List of object dictionaries
        """
        try:
            objects = []
            paginator = self.s3_client.get_paginator('list_objects_v2')
            
            pages = paginator.paginate(
                Bucket=self.config.bucket,
                Prefix=prefix
            )
            
            for page in pages:
                if 'Contents' in page:
                    objects.extend(page['Contents'])
            
            return objects
        except ClientError as e:
            logger.error(f"Error listing objects with prefix '{prefix}': {e}")
            self.stats['errors'] += 1
            return []

    def _search_gpkg_by_uuid(self, uuid: str) -> Optional[str]:
        """
        Search for GPKG file by UUID in S3.
        
        Args:
            uuid: UUID to search for
            
        Returns:
            S3 key of GPKG file or None if not found
        """
        logger.info(f"Searching for GPKG file with UUID: {uuid}")
        
        # Search for exact filename pattern
        search_key = f"{uuid}.gpkg"
        
        try:
            response = self.s3_client.head_object(
                Bucket=self.config.bucket,
                Key=search_key
            )
            logger.info(f"Found GPKG file: {search_key}")
            return search_key
        except ClientError:
            # File not found at root, search recursively
            logger.info(f"GPKG not found at root, searching in subdirectories...")
            
            objects = self._list_s3_objects()
            for obj in objects:
                if uuid in obj['Key'] and obj['Key'].lower().endswith('.gpkg'):
                    logger.info(f"Found GPKG file: {obj['Key']}")
                    return obj['Key']
            
            logger.warning(f"GPKG file not found for UUID: {uuid}")
            return None

    def _search_tif_folder_by_uuid(self, uuid: str) -> List[str]:
        """
        Search for TIF files in folder with UUID name.
        
        Args:
            uuid: UUID to search for (folder name)
            
        Returns:
            List of S3 keys for TIF files found
        """
        logger.info(f"Searching for TIF files in folder: {uuid}")
        
        objects = self._list_s3_objects(prefix=uuid)
        tif_files = [
            obj['Key'] for obj in objects
            if obj['Key'].lower().endswith(('.tif', '.tiff'))
        ]
        
        if tif_files:
            logger.info(f"Found {len(tif_files)} TIF file(s) in folder {uuid}")
        else:
            logger.warning(f"No TIF files found in folder: {uuid}")
        
        return tif_files

    def _get_all_objects_in_folder(self, folder_prefix: str) -> List[Dict]:
        """
        Get all objects (files) in a folder.
        
        Args:
            folder_prefix: S3 folder prefix
            
        Returns:
            List of object dictionaries
        """
        return self._list_s3_objects(prefix=folder_prefix)

    def _delete_s3_object(self, s3_key: str) -> bool:
        """
        Delete a single object from S3.
        
        Args:
            s3_key: S3 object key to delete
            
        Returns:
            True if successful, False otherwise
        """
        try:
            # Get object size before deletion for stats
            try:
                response = self.s3_client.head_object(
                    Bucket=self.config.bucket,
                    Key=s3_key
                )
                size = response['ContentLength']
            except ClientError:
                size = 0

            self.s3_client.delete_object(
                Bucket=self.config.bucket,
                Key=s3_key
            )
            
            self.stats['total_size_deleted'] += size
            logger.info(f"✓ Deleted: {s3_key} ({self._format_size(size)})")
            return True
        except ClientError as e:
            logger.error(f"✗ Failed to delete {s3_key}: {e}")
            self.stats['errors'] += 1
            return False

    def _delete_folder_recursively(self, folder_prefix: str) -> int:
        """
        Delete all objects in an S3 folder.
        
        Args:
            folder_prefix: S3 folder prefix (should end with /)
            
        Returns:
            Number of objects deleted
        """
        objects = self._get_all_objects_in_folder(folder_prefix)
        deleted_count = 0

        if not objects:
            logger.warning(f"No objects found in folder: {folder_prefix}")
            return 0

        logger.info(f"Deleting {len(objects)} object(s) from folder: {folder_prefix}")

        for obj in objects:
            if self._delete_s3_object(obj['Key']):
                deleted_count += 1

        return deleted_count

    def delete_gpkg_by_uuid(self, uuid: str, confirm: bool = False) -> bool:
        """
        Delete GPKG file by UUID.
        
        Args:
            uuid: UUID of the GPKG file to delete
            confirm: If False, ask for confirmation before deletion
            
        Returns:
            True if successful, False otherwise
        """
        gpkg_key = self._search_gpkg_by_uuid(uuid)
        
        if not gpkg_key:
            logger.error(f"GPKG file not found for UUID: {uuid}")
            return False

        # Show confirmation
        if not confirm:
            size_info = ""
            try:
                response = self.s3_client.head_object(
                    Bucket=self.config.bucket,
                    Key=gpkg_key
                )
                size_info = f" ({self._format_size(response['ContentLength'])})"
            except ClientError:
                pass

            print(f"\n⚠️  WARNING: About to delete GPKG file{size_info}")
            print(f"   Path: s3://{self.config.bucket}/{gpkg_key}")
            print(f"   UUID: {uuid}")
            response = input("\nType 'DELETE' to confirm deletion (or press Enter to cancel): ")
            
            if response.upper() != 'DELETE':
                logger.info("Deletion cancelled by user")
                return False

        # Perform deletion
        if self._delete_s3_object(gpkg_key):
            self.stats['gpkg_deleted'] += 1
            return True
        return False

    def delete_tif_folder_by_uuid(self, uuid: str, confirm: bool = False) -> bool:
        """
        Delete TIF folder and all files within by UUID.
        
        Args:
            uuid: UUID (folder name) to delete
            confirm: If False, ask for confirmation before deletion
            
        Returns:
            True if successful, False otherwise
        """
        # Ensure folder prefix ends with /
        folder_prefix = f"{uuid}/" if not uuid.endswith('/') else uuid
        
        objects = self._get_all_objects_in_folder(uuid)
        
        if not objects:
            logger.error(f"No files found in folder: {uuid}")
            return False

        # Filter for TIF files
        tif_files = [obj for obj in objects if obj['Key'].lower().endswith(('.tif', '.tiff'))]
        other_files = [obj for obj in objects if not obj['Key'].lower().endswith(('.tif', '.tiff'))]

        if not tif_files:
            logger.warning(f"No TIF files found in folder: {uuid}")
            return False

        # Calculate total size
        total_size = sum(obj['Size'] for obj in objects)

        # Show confirmation
        if not confirm:
            print(f"\n⚠️  WARNING: About to delete folder and all contents")
            print(f"   Path: s3://{self.config.bucket}/{uuid}/")
            print(f"   UUID: {uuid}")
            print(f"   Total files: {len(objects)} ({self._format_size(total_size)})")
            print(f"   TIF files: {len(tif_files)}")
            if other_files:
                print(f"   Other files: {len(other_files)}")
            response = input("\nType 'DELETE' to confirm deletion (or press Enter to cancel): ")
            
            if response.upper() != 'DELETE':
                logger.info("Deletion cancelled by user")
                return False

        # Perform deletion
        logger.info(f"Deleting folder {uuid} with {len(objects)} file(s)...")
        deleted_count = 0

        for obj in objects:
            if self._delete_s3_object(obj['Key']):
                deleted_count += 1
                if obj['Key'].lower().endswith(('.tif', '.tiff')):
                    self.stats['tif_deleted'] += 1

        if deleted_count == len(objects):
            self.stats['folders_deleted'] += 1
            return True
        
        return False

    def delete_by_uuid(self, uuid: str, delete_type: str = 'both', confirm: bool = False) -> bool:
        """
        Delete GPKG or TIF folder by UUID.
        
        Args:
            uuid: UUID to delete
            delete_type: 'gpkg', 'tif', or 'both'
            confirm: Skip confirmation if True
            
        Returns:
            True if at least one deletion was successful
        """
        success = False

        if delete_type in ['gpkg', 'both']:
            if self.delete_gpkg_by_uuid(uuid, confirm):
                success = True

        if delete_type in ['tif', 'both']:
            if self.delete_tif_folder_by_uuid(uuid, confirm):
                success = True

        return success

    def _print_summary(self) -> None:
        """Print deletion summary statistics."""
        logger.info("=" * 60)
        logger.info("DELETION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"GPKG files deleted: {self.stats['gpkg_deleted']}")
        logger.info(f"TIF files deleted: {self.stats['tif_deleted']}")
        logger.info(f"Folders deleted: {self.stats['folders_deleted']}")
        logger.info(f"Total size deleted: {self._format_size(self.stats['total_size_deleted'])}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info("=" * 60)


def load_config(config_file: str = 'config.json') -> S3Config:
    """
    Load S3 configuration from a JSON file in the script directory.

    Args:
        config_file: Name of the config file (default: config.json)

    Returns:
        S3Config object

    Raises:
        FileNotFoundError: If the config file does not exist
        ValueError: If the config file is invalid or missing required keys
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(script_dir, config_file)

    if not os.path.exists(config_path):
        raise FileNotFoundError(
            f"Configuration file not found: {config_path}\n"
            "Please create config.json with bucket, region, aws_access_key_id "
            "and aws_secret_access_key."
        )

    try:
        with open(config_path, 'r') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {config_path}: {e}")

    required = ['bucket', 'region', 'aws_access_key_id', 'aws_secret_access_key']
    missing = [key for key in required if not data.get(key)]
    if missing:
        raise ValueError(
            f"Missing required config in {config_path}: {', '.join(missing)}"
        )

    logger.info(f"Loaded configuration from: {config_path}")

    return S3Config(
        bucket=data['bucket'],
        region=data['region'],
        aws_access_key_id=data['aws_access_key_id'],
        aws_secret_access_key=data['aws_secret_access_key'],
    )


def interactive_mode(deleter: S3Deleter):
    """Interactive mode for deletion."""
    while True:
        print("\n" + "=" * 60)
        print("S3 DELETION TOOL - Interactive Mode")
        print("=" * 60)
        print("\n1. Delete GPKG file by UUID")
        print("2. Delete TIF folder by UUID")
        print("3. Delete both GPKG and TIF folder by UUID")
        print("4. Exit")
        
        choice = input("\nSelect an option (1-4): ").strip()
        
        if choice == '1':
            uuid = input("Enter UUID for GPKG file: ").strip()
            if uuid:
                deleter.delete_gpkg_by_uuid(uuid)
        
        elif choice == '2':
            uuid = input("Enter UUID for TIF folder: ").strip()
            if uuid:
                deleter.delete_tif_folder_by_uuid(uuid)
        
        elif choice == '3':
            uuid = input("Enter UUID: ").strip()
            if uuid:
                deleter.delete_by_uuid(uuid, delete_type='both')
        
        elif choice == '4':
            deleter._print_summary()
            print("\nExiting...")
            break
        
        else:
            print("Invalid option. Please try again.")


def main():
    """Main entry point."""
    try:
        config = load_config()
        deleter = S3Deleter(config)

        # Check if UUID is provided as command-line argument
        if len(sys.argv) > 1:
            uuid = sys.argv[1]
            delete_type = sys.argv[2] if len(sys.argv) > 2 else 'both'
            
            # Optional: --confirm flag to skip confirmation
            confirm = '--confirm' in sys.argv
            
            logger.info(f"Deleting {delete_type} for UUID: {uuid}")
            deleter.delete_by_uuid(uuid, delete_type=delete_type, confirm=confirm)
            deleter._print_summary()
        else:
            # Interactive mode
            interactive_mode(deleter)

    except (FileNotFoundError, ValueError) as e:
        logger.error(f"Configuration error: {e}")
        sys.exit(1)
    except KeyboardInterrupt:
        logger.info("\nOperation cancelled by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
